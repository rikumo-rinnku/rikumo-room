#!/usr/bin/env python3
"""Consistent online SQLite backup; retain the most recent 14 successful snapshots."""
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3

state = Path('/var/lib/rikumo-room')
source = state / 'rikumo-room.sqlite3'
backups = state / 'backups'
if not source.is_file():
    raise SystemExit('Database is missing; refusing to create an empty backup.')
os.umask(0o077)
backups.mkdir(mode=0o700, exist_ok=True)
name = datetime.now(timezone.utc).strftime('rikumo-%Y%m%dT%H%M%S%fZ.sqlite3')
target = backups / name
partial = target.with_suffix('.partial')
try:
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as origin:
        with sqlite3.connect(partial) as destination:
            origin.backup(destination)
            if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed.')
    partial.replace(target)
except Exception:
    partial.unlink(missing_ok=True)
    raise
for old in sorted(backups.glob('rikumo-*.sqlite3'), reverse=True)[14:]:
    old.unlink()
print(f'Database backup saved: {target.name}')
