#!/usr/bin/env bash
# Local daily backup on the VPS. Copy snapshots off the VPS separately.
set -euo pipefail
umask 077
# Install a root-owned copy at /usr/local/libexec/kyskinbot-backup.sh.
# Never execute app-owned Python, scripts or imports with root privileges.
/usr/bin/python3 - <<'PY'
from pathlib import Path
from datetime import datetime, timezone
from contextlib import closing
import os, sqlite3
os.umask(0o077)
root=Path('/opt/kyskinbot')
destination=Path('/var/backups/kyskinbot')/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
destination.mkdir(mode=0o700)
for name in ('history.sqlite3','data/envelopes.sqlite3'):
    target=destination/name
    target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with closing(sqlite3.connect((root/name).as_uri()+'?mode=ro',uri=True)) as source:
        with closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
            if copy.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:
                raise RuntimeError('Backup integrity check failed')
    target.chmod(0o600)
(destination/'.env').write_bytes((root/'.env').read_bytes())
(destination/'.env').chmod(0o600)
print('Daily database and configuration backup completed.')
PY
