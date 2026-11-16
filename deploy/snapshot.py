"""Consistent SQLite snapshots for migration; .env and media are transferred separately."""

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATABASES = ('history.sqlite3', 'data/envelopes.sqlite3')


def snapshot(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or source in destination.parents:
        raise ValueError('Keep snapshots outside the application directory')
    # An existing destination is never overwritten, including a previous snapshot.
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(destination, 0o700)
    for relative in DATABASES:
        origin, target = source / relative, destination / relative
        if not origin.is_file():
            raise ValueError(f'Missing database: {relative}')
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(origin.as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(target)) as copy:
                original.backup(copy)
                if copy.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError(f'Database integrity check failed: {relative}')
        os.chmod(target, 0o600)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        snapshot(ROOT, args.destination)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f'Snapshot failed: {error}\n')
    print('Snapshot ready. For final migration, stop the Mac bot before taking this snapshot.')
