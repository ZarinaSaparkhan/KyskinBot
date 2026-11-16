"""Independent attention requests for each partner; demos never become real requests."""

import json
import uuid


class AttentionStore:
    def __init__(self, database):
        self.database = database
        with database.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS attention_drafts (
                    user_id INTEGER PRIMARY KEY, token TEXT NOT NULL, kind TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attention_requests (
                    id TEXT PRIMARY KEY, sender INTEGER NOT NULL, recipient INTEGER NOT NULL,
                    kind TEXT NOT NULL, demo INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending', revision INTEGER NOT NULL DEFAULT 0,
                    delivery TEXT NOT NULL DEFAULT 'pending', reply_delivery TEXT NOT NULL DEFAULT 'none'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS attention_one_active
                    ON attention_requests(sender,demo) WHERE status!='done';
                CREATE TABLE IF NOT EXISTS attention_recordings (
                    user_id INTEGER PRIMARY KEY, payload TEXT NOT NULL
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(attention_requests)')}
            for name, definition in [('reply_file_id', 'TEXT'), ('media_delivery', "TEXT NOT NULL DEFAULT 'none'")]:
                if name not in columns:
                    db.execute(f'ALTER TABLE attention_requests ADD COLUMN {name} {definition}')

    def recording(self, user_id):
        with self.database.connect() as db:
            row = db.execute('SELECT payload FROM attention_recordings WHERE user_id=?', (user_id,)).fetchone()
        return json.loads(row['payload']) if row else None

    def save_recording(self, user_id, draft):
        draft['control_id'] = uuid.uuid4().hex
        with self.database.connect() as db:
            db.execute('INSERT OR REPLACE INTO attention_recordings VALUES (?,?)', (user_id, json.dumps(draft)))

    def discard_recording(self, user_id):
        with self.database.connect() as db:
            db.execute('DELETE FROM attention_recordings WHERE user_id=?', (user_id,))

    def pause_recording(self, user_id):
        draft = self.recording(user_id)
        if draft and draft.get('active'):
            draft['active'] = False
            self.save_recording(user_id, draft)

    def confirm_recording(self, user_id, control_id, *, text_only=False):
        """Commit the chosen reply and attachment once, before any Telegram delivery."""
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM attention_recordings WHERE user_id=?', (user_id,)).fetchone()
            draft = json.loads(row['payload']) if row else None
            if not draft or draft['control_id'] != control_id or not draft.get('active'):
                return None
            request = db.execute('SELECT * FROM attention_requests WHERE id=? AND recipient=?',
                                 (draft['request_id'], user_id)).fetchone()
            if (not request or request['status'] == 'done' or request['revision'] != draft['revision']
                    or request['kind'] not in {'circle', 'voice'} or draft['kind'] != request['kind'] or request['reply_file_id']):
                return None
            if not text_only and (not draft.get('file_id') or draft.get('retaking')):
                return None
            file_id = None if text_only else draft['file_id']
            reply_delivery = request['reply_delivery'] if request['status'] == 'ready' else 'pending'
            db.execute('''UPDATE attention_requests SET status='ready',revision=revision+1,
                reply_file_id=?,media_delivery=?,reply_delivery=? WHERE id=?''',
                (file_id, 'pending' if file_id else 'none', reply_delivery, request['id']))
            db.execute('DELETE FROM attention_recordings WHERE user_id=?', (user_id,))
            result = db.execute('SELECT * FROM attention_requests WHERE id=?', (request['id'],)).fetchone()
        return dict(result)

    def save_draft(self, user_id, token, kind):
        with self.database.connect() as db:
            db.execute('INSERT OR REPLACE INTO attention_drafts VALUES (?,?,?)', (user_id, token, kind))

    def draft(self, user_id):
        with self.database.connect() as db:
            row = db.execute('SELECT * FROM attention_drafts WHERE user_id=?', (user_id,)).fetchone()
        return dict(row) if row else None

    def clear_draft(self, user_id):
        with self.database.connect() as db:
            db.execute('DELETE FROM attention_drafts WHERE user_id=?', (user_id,))

    def get(self, request_id, user_id):
        with self.database.connect() as db:
            row = db.execute('SELECT * FROM attention_requests WHERE id=? AND (sender=? OR recipient=?)',
                             (request_id, user_id, user_id)).fetchone()
        return dict(row) if row else None

    def outgoing(self, user_id, demo):
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM attention_requests WHERE sender=? AND demo=? AND status!='done'",
                             (user_id, int(demo))).fetchone()
        return dict(row) if row else None

    def incoming(self, user_id):
        with self.database.connect() as db:
            rows = db.execute("SELECT * FROM attention_requests WHERE recipient=? AND demo=0 AND status!='done'", (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def create(self, user_id, recipient, token, demo):
        with self.database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            draft = db.execute('SELECT * FROM attention_drafts WHERE user_id=? AND token=?', (user_id, token)).fetchone()
            if draft is None:
                return None, False
            existing = db.execute("SELECT * FROM attention_requests WHERE sender=? AND demo=? AND status!='done'",
                                  (user_id, int(demo))).fetchone()
            if existing:
                db.execute('DELETE FROM attention_drafts WHERE user_id=?', (user_id,))
                return dict(existing), False
            db.execute('INSERT INTO attention_requests (id,sender,recipient,kind,demo,delivery) VALUES (?,?,?,?,?,?)',
                       (token, user_id, recipient, draft['kind'], int(demo), 'sent' if demo else 'pending'))
            db.execute('DELETE FROM attention_drafts WHERE user_id=?', (user_id,))
            row = db.execute('SELECT * FROM attention_requests WHERE id=?', (token,)).fetchone()
        return dict(row), True

    def delivery(self, request_id, state, *, reply=False):
        column = 'reply_delivery' if reply else 'delivery'
        with self.database.connect() as db:
            db.execute(f'UPDATE attention_requests SET {column}=? WHERE id=?', (state, request_id))

    def media_delivery(self, request_id, state):
        with self.database.connect() as db:
            db.execute('UPDATE attention_requests SET media_delivery=? WHERE id=?', (state, request_id))

    def respond(self, request_id, user_id, revision, status):
        with self.database.connect() as db:
            changed = db.execute('''UPDATE attention_requests SET status=?,revision=revision+1,reply_delivery='pending'
                WHERE id=? AND recipient=? AND revision=? AND status IN ('pending','later','planning')''',
                (status, request_id, user_id, revision)).rowcount
            if changed:
                db.execute("DELETE FROM attention_recordings WHERE user_id=? AND json_extract(payload,'$.request_id')=?", (user_id, request_id))
        return bool(changed)

    def finish(self, request_id, user_id):
        with self.database.connect() as db:
            db.execute("UPDATE attention_requests SET status='done',revision=revision+1 WHERE id=? AND (sender=? OR recipient=?)",
                       (request_id, user_id, user_id))
            request = db.execute('SELECT id FROM attention_requests WHERE id=? AND (sender=? OR recipient=?)', (request_id, user_id, user_id)).fetchone()
            if request:
                db.execute("DELETE FROM attention_recordings WHERE json_extract(payload,'$.request_id')=?", (request_id,))
