"""Migration checks with temporary databases; no real credentials or Telegram calls."""

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


snapshot = load('snapshot')
preflight = load('preflight')


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / 'app'
        (self.root / 'data').mkdir(parents=True)
        (self.root / 'media').mkdir()
        (self.root / 'media' / 'cat.jpg').write_bytes(b'prepared photo fixture')
        (self.root / '.env').write_text('BOT_TOKEN=123:OFFLINE\nALLOWED_USER_IDS=101\n')
        self.connections = []
        for relative, names in [
            ('history.sqlite3', ['sent_history', 'current_cycle', 'active_views']),
            ('data/envelopes.sqlite3', ['envelopes', 'envelope_drafts', 'envelope_invites', 'attention_requests']),
        ]:
            db = sqlite3.connect(self.root / relative)
            db.execute('PRAGMA journal_mode=WAL')
            for name in names:
                db.execute(f'CREATE TABLE {name} (value TEXT)')
            db.commit()
            self.connections.append(db)

    def tearDown(self):
        for db in self.connections:
            db.close()
        self.directory.cleanup()

    def test_snapshot_keeps_committed_wal_and_excludes_uncommitted_rows(self):
        history = self.connections[0]
        history.execute("INSERT INTO sent_history VALUES ('committed')")
        history.commit()
        writer = sqlite3.connect(self.root / 'history.sqlite3')
        try:
            writer.execute("INSERT INTO sent_history VALUES ('not committed')")
            target = Path(self.directory.name) / 'snapshot'
            snapshot.snapshot(self.root, target)
            with closing(sqlite3.connect(target / 'history.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT value FROM sent_history').fetchall(), [('committed',)])
            self.assertTrue((target / 'data' / 'envelopes.sqlite3').is_file())
            self.assertFalse((target / '.env').exists())
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(target / 'history.sqlite3').st_mode & 0o777, 0o600)
        finally:
            writer.rollback()
            writer.close()

    def test_snapshot_never_overwrites_existing_directory_or_application(self):
        for target in [self.root, self.root / 'backup', Path(self.directory.name)]:
            with self.assertRaises((ValueError, FileExistsError)):
                snapshot.snapshot(self.root, target)
        self.assertTrue((self.root / '.env').is_file())

    def test_preflight_accepts_complete_migration_without_mutating_databases(self):
        before = [(self.root / name).stat().st_mtime_ns for name in snapshot.DATABASES]
        self.assertEqual(preflight.check(self.root), 1)
        after = [(self.root / name).stat().st_mtime_ns for name in snapshot.DATABASES]
        self.assertEqual(before, after)

    def test_preflight_rejects_missing_state_and_missing_farewell_photos(self):
        (self.root / 'media' / 'cat.jpg').unlink()
        with self.assertRaisesRegex(ValueError, 'photos'):
            preflight.check(self.root)
        (self.root / 'media' / 'cat.jpg').write_bytes(b'fixture')
        self.connections[1].execute('DROP TABLE envelope_invites')
        self.connections[1].commit()
        with self.assertRaisesRegex(ValueError, 'Incomplete migrated database'):
            preflight.check(self.root)


if __name__ == '__main__':
    unittest.main()
