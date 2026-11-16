"""Durable drafts, envelopes and recipient notification preferences."""

import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager
import hashlib


class EnvelopeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS envelope_drafts (
                    user_id INTEGER PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS envelope_settings (
                    user_id INTEGER PRIMARY KEY, zone TEXT, notifications INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS envelopes (
                    id TEXT PRIMARY KEY, sender INTEGER NOT NULL, recipient INTEGER NOT NULL,
                    release_at REAL NOT NULL, payload TEXT NOT NULL,
                    opened INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0,
                    notice_lease REAL NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS envelopes_recipient ON envelopes(recipient, release_at);
                CREATE TABLE IF NOT EXISTS envelope_users (user_id INTEGER PRIMARY KEY, username TEXT);
                CREATE TABLE IF NOT EXISTS envelope_invites (
                    owner_id INTEGER PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, claimed_by INTEGER
                );
                CREATE TABLE IF NOT EXISTS gift_welcome_media (
                    owner_id INTEGER PRIMARY KEY, file_id TEXT, draft TEXT
                );
                CREATE TABLE IF NOT EXISTS gift_welcome_delivery (
                    recipient INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL,
                    greeting TEXT NOT NULL, file_id TEXT,
                    text_sent INTEGER NOT NULL DEFAULT 0,
                    circle_sent INTEGER NOT NULL DEFAULT 0,
                    blocked INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(envelopes)')}
            if 'cancelled' not in columns:
                db.execute('ALTER TABLE envelopes ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(gift_welcome_delivery)')}
            if 'owner_notified' not in columns:
                db.execute('ALTER TABLE gift_welcome_delivery ADD COLUMN owner_notified INTEGER NOT NULL DEFAULT 0')
                # Already joined partners must not generate a new-entry alert on upgrade.
                db.execute('UPDATE gift_welcome_delivery SET owner_notified=1')
            if 'owner_notice_blocked' not in columns:
                db.execute('ALTER TABLE gift_welcome_delivery ADD COLUMN owner_notice_blocked INTEGER NOT NULL DEFAULT 0')
            if 'owner_notice_retry_at' not in columns:
                db.execute('ALTER TABLE gift_welcome_delivery ADD COLUMN owner_notice_retry_at REAL NOT NULL DEFAULT 0')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def invited_partner(self, owner_id):
        with self.connect() as db:
            row = db.execute('SELECT claimed_by FROM envelope_invites WHERE owner_id=?', (owner_id,)).fetchone()
        return row['claimed_by'] if row else None

    def create_invite(self, owner_id, token):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO envelope_invites VALUES (?,?,NULL)',
                       (owner_id, hashlib.sha256(token.encode()).hexdigest()))

    def claim_invite(self, owner_id, user_id, token):
        if user_id == owner_id:
            return False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT claimed_by FROM envelope_invites WHERE owner_id=? AND token_hash=?',
                             (owner_id, hashlib.sha256(token.encode()).hexdigest())).fetchone()
            if not row or row['claimed_by'] not in (None, user_id):
                return False
            db.execute('UPDATE envelope_invites SET claimed_by=? WHERE owner_id=?', (user_id, owner_id))
            db.execute('UPDATE envelopes SET recipient=? WHERE sender=? AND recipient=0', (user_id, owner_id))
        return True

    def draft(self, user_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM envelope_drafts WHERE user_id=?', (user_id,)).fetchone()
        return json.loads(row['payload']) if row else None

    def save_draft(self, user_id, draft):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO envelope_drafts VALUES (?,?)',
                       (user_id, json.dumps(draft, ensure_ascii=False)))

    def discard_draft(self, user_id):
        with self.connect() as db:
            return db.execute('DELETE FROM envelope_drafts WHERE user_id=?', (user_id,)).rowcount > 0

    def pause(self, user_id):
        draft = self.draft(user_id)
        if draft:
            draft['active'] = False
            self.save_draft(user_id, draft)
        self.pause_gift_draft(user_id)

    def gift_circle(self, owner_id):
        with self.connect() as db:
            row = db.execute('SELECT file_id FROM gift_welcome_media WHERE owner_id=?', (owner_id,)).fetchone()
        return row['file_id'] if row else None

    def gift_draft(self, owner_id):
        with self.connect() as db:
            row = db.execute('SELECT draft FROM gift_welcome_media WHERE owner_id=?', (owner_id,)).fetchone()
        return json.loads(row['draft']) if row and row['draft'] else None

    def save_gift_draft(self, owner_id, draft):
        with self.connect() as db:
            db.execute('''INSERT INTO gift_welcome_media(owner_id,draft) VALUES (?,?)
                ON CONFLICT(owner_id) DO UPDATE SET draft=excluded.draft''',
                       (owner_id, json.dumps(draft, ensure_ascii=False)))

    def discard_gift_draft(self, owner_id):
        with self.connect() as db:
            db.execute('UPDATE gift_welcome_media SET draft=NULL WHERE owner_id=?', (owner_id,))

    def pause_gift_draft(self, owner_id):
        draft = self.gift_draft(owner_id)
        if draft:
            draft['active'] = False
            self.save_gift_draft(owner_id, draft)

    def confirm_gift_circle(self, owner_id, control_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT draft FROM gift_welcome_media WHERE owner_id=?', (owner_id,)).fetchone()
            draft = json.loads(row['draft']) if row and row['draft'] else None
            if (not draft or draft.get('control_id') != control_id
                    or draft.get('step') not in {'review', 'remove'}
                    or draft['step'] == 'review' and not draft.get('file_id')):
                return False
            db.execute('UPDATE gift_welcome_media SET file_id=?,draft=NULL WHERE owner_id=?',
                       (draft.get('file_id') if draft['step'] == 'review' else None, owner_id))
        return True

    def prepare_gift_delivery(self, owner_id, recipient, greeting):
        with self.connect() as db:
            db.execute('''INSERT OR IGNORE INTO gift_welcome_delivery
                (recipient,owner_id,greeting,file_id,circle_sent)
                SELECT ?,?,?,file_id,CASE WHEN file_id IS NULL THEN 1 ELSE 0 END
                FROM gift_welcome_media WHERE owner_id=?''', (recipient, owner_id, greeting, owner_id))
            db.execute('''INSERT OR IGNORE INTO gift_welcome_delivery
                (recipient,owner_id,greeting,circle_sent) VALUES (?,?,?,1)''', (recipient, owner_id, greeting))

    def gift_delivery(self, recipient):
        with self.connect() as db:
            row = db.execute('SELECT * FROM gift_welcome_delivery WHERE recipient=?', (recipient,)).fetchone()
        return dict(row) if row else None

    def gift_delivery_result(self, recipient, *, text_sent=False, circle_sent=False, blocked=False, retry_at=0):
        with self.connect() as db:
            db.execute('''UPDATE gift_welcome_delivery SET text_sent=MAX(text_sent,?),
                circle_sent=MAX(circle_sent,?),blocked=?,retry_at=? WHERE recipient=?''',
                       (int(text_sent), int(circle_sent), int(blocked), retry_at, recipient))

    def gift_owner_notice_result(self, recipient, *, sent=False, blocked=False, retry_at=0):
        with self.connect() as db:
            db.execute('''UPDATE gift_welcome_delivery SET owner_notified=MAX(owner_notified,?),
                owner_notice_blocked=?,owner_notice_retry_at=? WHERE recipient=?''',
                       (int(sent), int(blocked), retry_at, recipient))

    def settings(self, user_id):
        with self.connect() as db:
            row = db.execute('SELECT zone,notifications FROM envelope_settings WHERE user_id=?', (user_id,)).fetchone()
        return dict(row) if row else {'zone': None, 'notifications': 0}

    def remember_user(self, user):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO envelope_users VALUES (?,?)', (user.id, user.username))

    def username(self, user_id):
        with self.connect() as db:
            row = db.execute('SELECT username FROM envelope_users WHERE user_id=?', (user_id,)).fetchone()
        return row['username'] if row else None

    def set_setting(self, user_id, *, zone=None, notifications=None):
        current = self.settings(user_id)
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO envelope_settings VALUES (?,?,?)',
                       (user_id, zone or current['zone'],
                        current['notifications'] if notifications is None else int(notifications)))

    def seal(self, sender, recipient, draft):
        # One transaction prevents a second press from creating another envelope.
        with self.connect() as db:
            result = db.execute('INSERT OR IGNORE INTO envelopes (id,sender,recipient,release_at,payload) VALUES (?,?,?,?,?)',
                                (draft['id'], sender, recipient, draft['release_at'], json.dumps(draft, ensure_ascii=False)))
            db.execute('DELETE FROM envelope_drafts WHERE user_id=?', (sender,))
            return result.rowcount == 1

    @staticmethod
    def unpack(row):
        if row is None:
            return None
        result = json.loads(row['payload'])
        result.update({key: row[key] for key in ('id', 'sender', 'recipient', 'release_at', 'opened', 'cancelled')})
        return result

    def get(self, envelope_id, user_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM envelopes WHERE id=? AND (sender=? OR (recipient=? AND cancelled=0))',
                             (envelope_id, user_id, user_id)).fetchone()
        return self.unpack(row)

    def listing(self, user_id, direction, offset=0, limit=8):
        column = 'sender' if direction == 'sent' else 'recipient'
        visible = '' if direction == 'sent' else ' AND cancelled=0'
        with self.connect() as db:
            rows = db.execute(f'SELECT * FROM envelopes WHERE {column}=?{visible} ORDER BY rowid DESC LIMIT ? OFFSET ?',
                              (user_id, limit + 1, offset)).fetchall()
        return [self.unpack(row) for row in rows[:limit]], len(rows) > limit

    def mark_opened(self, envelope_id, recipient):
        with self.connect() as db:
            db.execute('UPDATE envelopes SET opened=1 WHERE id=? AND recipient=? AND cancelled=0', (envelope_id, recipient))

    def cancel(self, envelope_id, sender):
        with self.connect() as db:
            return db.execute('''UPDATE envelopes SET cancelled=1,notified=2
                WHERE id=? AND sender=? AND opened=0 AND cancelled=0''',
                (envelope_id, sender)).rowcount > 0

    def claim_due(self, now):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('''SELECT e.* FROM envelopes e
                JOIN envelope_settings s ON s.user_id=e.recipient
                WHERE e.release_at<=? AND e.opened=0 AND e.notified=0 AND e.cancelled=0
                  AND e.notice_lease<=? AND s.notifications=1 LIMIT 10''', (now, now)).fetchall()
            for row in rows:
                db.execute('UPDATE envelopes SET notice_lease=? WHERE id=?', (now + 300, row['id']))
        return [self.unpack(row) for row in rows]

    def notification_result(self, envelope_id, *, sent=False, blocked=False, retry_at=0):
        with self.connect() as db:
            db.execute('UPDATE envelopes SET notified=?,notice_lease=? WHERE id=?',
                       (2 if blocked else int(sent), retry_at, envelope_id))
