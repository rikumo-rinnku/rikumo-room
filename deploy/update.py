#!/usr/bin/env python3
"""Build an isolated release, back up SQLite, switch app paths and retain old code."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

from deploy import ROOT, STATE, USER, run, write


def ready() -> None:
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2) as response:
                if response.status == 200 and json.load(response).get('status') == 'ok':
                    return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError('API did not become ready; inspect journalctl -u rikumo-api.')


def backup() -> Path:
    directory = STATE / 'update-backups'
    directory.mkdir(mode=0o700, exist_ok=True)
    destination = directory / f'pre-update-{time.time_ns()}.sqlite3'
    database = STATE / 'rikumo-room.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as origin:
        with sqlite3.connect(destination) as snapshot:
            origin.backup(snapshot)
            if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Pre-update database backup failed integrity check.')
    destination.chmod(0o600)
    return destination


def switch(path: Path, target: Path, previous: Path) -> None:
    # Only move the explicitly named app paths, never the database or configuration.
    path.rename(previous)
    try:
        path.symlink_to(target, target_is_directory=True)
    except Exception:
        previous.rename(path)
        raise


def restore(path: Path, previous: Path) -> None:
    if not path.is_symlink():
        raise RuntimeError(f'Refusing to replace unexpected non-symlink: {path}')
    path.unlink()
    previous.rename(path)


def main() -> None:
    parser = argparse.ArgumentParser(description='Update existing Rikumo installation; retain configuration and database.')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--web-only', action='store_true', help='Update only frontend, without restarting API')
    parser.add_argument('--prebuilt-web', action='store_true', help='Use locally built frontend included in the upload')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('Run on the installed Ubuntu server as root or through sudo.')
    source = args.source.resolve()
    for path in (ROOT / '.env', STATE / 'rikumo-room.sqlite3', ROOT / 'web/dist/index.html'):
        if not path.is_file():
            parser.error(f'Existing installation missing {path}; use initial installer first.')
    for relative in ('server/app/main.py', 'server/pyproject.toml', 'web/package-lock.json'):
        if not (source / relative).is_file():
            parser.error(f'Update source missing {relative}.')
    if args.prebuilt_web and not (source / 'web/dist/index.html').is_file():
        parser.error('Prebuilt frontend missing dist/index.html; refusing to switch versions.')
    lock = open('/run/lock/rikumo-update.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('Another update is running.')
    os.umask(0o022)
    releases = ROOT / '.releases'
    releases.mkdir(mode=0o755, exist_ok=True)
    release = releases / uuid.uuid4().hex
    release.mkdir()
    shutil.copytree(source / 'web', release / 'web', ignore=shutil.ignore_patterns('node_modules', 'dist', '.env', '.env.*', '*.tsbuildinfo'))
    if args.prebuilt_web:
        shutil.copytree(source / 'web/dist', release / 'web/dist')
    if not args.web_only:
        shutil.copytree(source / 'server/app', release / 'server/app', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(source / 'server/pyproject.toml', release / 'server/pyproject.toml')
    (release / '.env').symlink_to(ROOT / '.env')
    home = release / '.build-home'
    home.mkdir()
    run('chown', '-R', f'{USER}:{USER}', str(release))
    print('准备新版本：切换前仍由旧版本提供服务。', flush=True)
    if args.prebuilt_web:
        print('使用发布包内的网页构建结果，跳过服务器 npm 下载与构建。', flush=True)
    else:
        run('runuser', '-u', USER, '--', 'env', f'HOME={home}', 'npm', 'ci', cwd=release / 'web')
        run('runuser', '-u', USER, '--', 'env', f'HOME={home}', 'npm', 'run', 'build', cwd=release / 'web')
    if not args.web_only:
        run('runuser', '-u', USER, '--', 'python3', '-m', 'venv', str(release / 'server/.venv'))
        run('runuser', '-u', USER, '--', str(release / 'server/.venv/bin/pip'), 'install', str(release / 'server'))
    run('nginx', '-t')
    previous_server = release / 'previous-server'
    previous_dist = release / 'previous-dist'
    switched_server = switched_dist = stopped = False
    snapshot = None
    try:
        if not args.web_only:
            run('systemctl', 'stop', 'rikumo-api')
            stopped = True
        snapshot = backup()
        print(f'更新前数据库快照：{snapshot}', flush=True)
        if not args.web_only:
            switch(ROOT / 'server', release / 'server', previous_server)
            switched_server = True
            run('systemctl', 'start', 'rikumo-api')
            ready()
        switch(ROOT / 'web/dist', release / 'web/dist', previous_dist)
        switched_dist = True
        run('systemctl', 'reload', 'nginx')
    except Exception:
        print('切换失败，正在恢复上一版代码；数据库不自动回退以避免丢失新数据。', flush=True)
        if switched_dist:
            restore(ROOT / 'web/dist', previous_dist)
        if switched_server:
            run('systemctl', 'stop', 'rikumo-api')
            restore(ROOT / 'server', previous_server)
        if stopped:
            run('systemctl', 'start', 'rikumo-api')
        run('systemctl', 'reload', 'nginx')
        raise
    write(release / 'update-info.json', json.dumps({
        'release': str(release), 'web_only': args.web_only,
        'database_backup': str(snapshot), 'previous_server': str(previous_server) if switched_server else None,
        'previous_dist': str(previous_dist),
    }, indent=2), 0o600)
    print('更新完成。浏览器刷新后使用新页面；账号、密钥、数据库和媒体服务配置保留。')
    print('旧版本及数据库快照保留在服务器，尚未自动清理。LiveKit 未重启。')
    lock.close()


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'更新中止：{error}\n请保留发布目录与数据库快照，按报错排查。', file=sys.stderr)
        sys.exit(1)
