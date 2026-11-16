"""Read-only startup checks. Never print token, private IDs or message contents."""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(root=ROOT):
    root = Path(root)
    env = dotenv_values(root / '.env')
    if not env.get('BOT_TOKEN'):
        raise ValueError('Missing BOT_TOKEN in .env')
    try:
        allowed = {int(value.strip()) for value in (env.get('ALLOWED_USER_IDS') or '').split(',') if value.strip()}
    except ValueError:
        raise ValueError('Invalid ALLOWED_USER_IDS') from None
    if len(allowed) not in {1, 2} or any(value <= 0 for value in allowed):
        raise ValueError('Configure one owner or two participants')
    for relative, tables in [
        ('history.sqlite3', {'sent_history', 'current_cycle', 'active_views'}),
        ('data/envelopes.sqlite3', {'envelopes', 'envelope_drafts', 'envelope_invites', 'attention_requests'}),
    ]:
        path = root / relative
        if not path.is_file():
            raise ValueError(f'Missing migrated database: {relative}')
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError(f'Database integrity check failed: {relative}')
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables <= names:
                raise ValueError(f'Incomplete migrated database: {relative}')
    # Enumerate prepared files without opening the actual photos or videos.
    media = root / 'media'
    files = [path for path in media.rglob('*') if path.is_file()
             and not any(part.startswith('.') for part in path.relative_to(media).parts)
             and path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.mp4', '.gif'}]
    if not any(path.suffix.lower() in {'.jpg', '.jpeg', '.png'} for path in files):
        raise ValueError('No prepared cat photos for the farewell photo')
    return len(files)


if __name__ == '__main__':
    try:
        count = check()
    except (OSError, ValueError, sqlite3.Error) as error:
        # Errors above describe structure, never private database rows or .env values.
        print(f'Preflight failed: {error}', file=sys.stderr)
        raise SystemExit(1)
    print(f'Preflight OK; prepared media files: {count}')
