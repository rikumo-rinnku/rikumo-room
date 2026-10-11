#!/usr/bin/env python3
"""Initial native deployment on a fresh Ubuntu 24.04 server. No third-party Python modules."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path('/opt/rikumo-room')
STATE = Path('/var/lib/rikumo-room')
USER = 'rikumo'
CERTBOT_HOME = Path('/opt/rikumo-certbot')
CERTBOT = str(CERTBOT_HOME / 'bin/certbot')


def run(*args: str, cwd: Path | None = None) -> None:
    # Argument arrays, never shell interpolation; secrets are not logged.
    subprocess.run(args, cwd=cwd, check=True)


def write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        os.fchmod(temp.fileno(), mode)
        temp.write(text.encode('utf-8'))
        temporary = temp.name
    os.replace(temporary, path)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': 'RikumoRoom-deploy'})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def install_livekit(version: str) -> None:
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]
    release = json.loads(fetch(f'https://api.github.com/repos/livekit/livekit/releases/tags/v{version}'))
    archive_name = f'livekit_{version}_linux_{arch}.tar.gz'
    asset = next((item for item in release['assets'] if item['name'] == archive_name), None)
    if not asset:
        raise RuntimeError(f'LiveKit release has no {archive_name}')
    blob = fetch(asset['browser_download_url'])
    digest = asset.get('digest') or ''
    if digest.startswith('sha256:'):
        expected = digest.removeprefix('sha256:')
    else:
        checksum = next((item for item in release['assets'] if 'checksums' in item['name'].lower()), None)
        if not checksum:
            raise RuntimeError('No release checksum available; refusing unverified binary.')
        lines = fetch(checksum['browser_download_url']).decode().splitlines()
        matches = [line.split()[0] for line in lines if line.split()[-1].lstrip('*') == archive_name]
        if len(matches) != 1:
            raise RuntimeError('LiveKit checksum entry missing.')
        expected = matches[0]
    if hashlib.sha256(blob).hexdigest() != expected:
        raise RuntimeError('LiveKit download checksum mismatch.')
    with tempfile.TemporaryFile() as temp:
        temp.write(blob)
        temp.seek(0)
        with tarfile.open(fileobj=temp, mode='r:gz') as archive:
            entry = next((item for item in archive if Path(item.name).name == 'livekit-server' and item.isfile()), None)
            if not entry:
                raise RuntimeError('LiveKit executable missing from archive.')
            executable = archive.extractfile(entry)
            assert executable is not None
            destination = Path('/usr/local/bin/livekit-server')
            destination.write_bytes(executable.read())
            destination.chmod(0o755)


def unit(description: str, command: str, working: str = str(ROOT), extra: str = '') -> str:
    return f'''[Unit]
Description={description}
After=network-online.target
Wants=network-online.target

[Service]
User={USER}
Group={USER}
WorkingDirectory={working}
ExecStart={command}
Restart=on-failure
RestartSec=3
TimeoutStopSec=60
UMask=0077
NoNewPrivileges=true
LimitNOFILE=65536
{extra}
[Install]
WantedBy=multi-user.target
'''


def main() -> None:
    parser = argparse.ArgumentParser(description='Ubuntu 24.04 initial deployment (run with sudo).')
    parser.add_argument('--domain', help='Optional DNS hostname; omit for public IP HTTPS testing')
    parser.add_argument('--public-ip', required=True, help='Server public IPv4')
    parser.add_argument('--email', help='Optional certificate renewal contact')
    parser.add_argument('--admin-name', default='璃雲')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--ssh-port', type=int, default=22)
    parser.add_argument('--livekit-version', default='1.13.7')
    parser.add_argument('--plan', action='store_true', help='Print plan only; no changes or downloads')
    args = parser.parse_args()
    domain = args.domain.lower().rstrip('.') if args.domain else args.public_ip
    if args.domain and not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', domain):
        parser.error('Provide a valid DNS hostname.')
    public_ip = ipaddress.IPv4Address(args.public_ip)
    if not public_ip.is_global:
        parser.error('--public-ip must be a public IPv4 address.')
    if args.email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', args.email):
        parser.error('Invalid certificate contact email.')
    if not 1 <= args.ssh_port <= 65535 or not 1 <= len(args.admin_name) <= 48:
        parser.error('Invalid SSH port or administrator name.')
    if any(character in args.admin_name for character in ('\n', '\r', '\x00')):
        parser.error('Administrator name cannot contain line breaks or NUL.')
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.livekit_version):
        parser.error('LiveKit version must have the form 1.13.7.')
    source = args.source.resolve()
    for required in ('server/app/main.py', 'server/pyproject.toml', 'web/package-lock.json', 'deploy/backup.py'):
        if not (source / required).is_file():
            parser.error(f'Missing source file: {required}')
    print(f'Deploy {domain} -> {public_ip}; application {ROOT}; database {STATE}')
    print(f'Required cloud firewall: TCP {args.ssh_port},80,443,7881; UDP 7882,3478')
    print('Native Python + LiveKit + Nginx; one API worker; daily SQLite backup; no Docker.')
    if args.plan:
        return
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('Run on the Ubuntu server as root or through sudo.')
    os_release = Path('/etc/os-release').read_text()
    if 'ID=ubuntu' not in os_release or 'VERSION_ID="24.04"' not in os_release:
        parser.error('This deployment script supports Ubuntu 24.04 only.')
    if platform.machine() not in ('x86_64', 'aarch64'):
        parser.error('Supported CPU architectures: x86_64 and aarch64.')
    if ROOT.exists() or STATE.exists() or CERTBOT_HOME.exists():
        parser.error('An installation/data directory already exists. This is an initial installer; preserve it and arrange an update or recovery instead.')
    enabled = Path('/etc/nginx/sites-enabled')
    if enabled.exists() and any(item.name != 'default' for item in enabled.iterdir()):
        parser.error('Existing Nginx sites detected. Arrange integration before deploying.')
    if args.domain and str(public_ip) not in {item[4][0] for item in socket.getaddrinfo(domain, 80, family=socket.AF_INET)}:
        parser.error('Domain A record does not point to --public-ip. Configure DNS first.')
    for port in (80, 443, 7880, 7881, 7882, 8000, 3478):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if port in (7882, 3478) else socket.SOCK_STREAM) as sock:
            try:
                sock.bind(('0.0.0.0', port))
            except OSError:
                parser.error(f'Port {port} is occupied. Use a fresh server or arrange integration.')
    password = getpass.getpass('超级管理员密码（至少4个字符，不回显）：')
    if len(password) < 4 or len(password) > 200 or '\n' in password or '\r' in password:
        parser.error('Password must contain 4–200 characters and no line breaks.')
    if password != getpass.getpass('再次输入密码：'):
        parser.error('Passwords do not match.')

    os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', 'python3-venv', 'python3-pip', 'nodejs', 'npm', 'nginx', 'ca-certificates', 'ufw')
    # Ubuntu's apt Certbot may be too old for IP certificates. Root-owned isolated runtime.
    run('python3', '-m', 'venv', str(CERTBOT_HOME))
    run(str(CERTBOT_HOME / 'bin/pip'), 'install', 'certbot>=5.4,<6')
    if subprocess.run(['id', '-u', USER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        run('useradd', '--system', '--home-dir', str(STATE), '--shell', '/usr/sbin/nologin', USER)
    ROOT.mkdir()
    STATE.mkdir(mode=0o750)
    # Copy application sources only, never development credentials, databases or certificates.
    shutil.copytree(source / 'server/app', ROOT / 'server/app', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(source / 'server/pyproject.toml', ROOT / 'server/pyproject.toml')
    shutil.copytree(source / 'web', ROOT / 'web', ignore=shutil.ignore_patterns('node_modules', 'dist', '.env', '.env.*', '*.tsbuildinfo'))
    run('chown', '-R', f'{USER}:{USER}', str(ROOT), str(STATE))
    run('runuser', '-u', USER, '--', 'python3', '-m', 'venv', str(ROOT / 'server/.venv'))
    run('runuser', '-u', USER, '--', str(ROOT / 'server/.venv/bin/pip'), 'install', str(ROOT / 'server'))
    # Build in a temporary HOME, leaving the service user's data permissions unchanged.
    cache_home = ROOT / '.build-home'
    cache_home.mkdir()
    run('chown', f'{USER}:{USER}', str(cache_home))
    run('runuser', '-u', USER, '--', 'env', f'HOME={cache_home}', 'npm', 'ci', cwd=ROOT / 'web')
    run('runuser', '-u', USER, '--', 'env', f'HOME={cache_home}', 'npm', 'run', 'build', cwd=ROOT / 'web')
    install_livekit(args.livekit_version)

    app_key = secrets.token_hex(32)
    lk_key = secrets.token_hex(12)
    lk_secret = secrets.token_hex(32)
    settings = {
        'APP_ENV': 'production', 'APP_SECRET': app_key,
        'DATABASE_PATH': str(STATE / 'rikumo-room.sqlite3'), 'APP_TIMEZONE': 'Asia/Shanghai',
        'SUPERADMIN_USERNAME': args.admin_name, 'SUPERADMIN_PASSWORD': password,
        'LIVEKIT_API_URL': 'http://127.0.0.1:7880', 'LIVEKIT_API_KEY': lk_key,
        'LIVEKIT_API_SECRET': lk_secret, 'LIVEKIT_PUBLIC_URL': f'wss://{domain}/livekit',
        'SESSION_COOKIE_SECURE': 'true',
    }
    write(ROOT / '.env', ''.join(f'{key}={json.dumps(value, ensure_ascii=False)}\n' for key, value in settings.items()), 0o640)
    run('chown', f'root:{USER}', str(ROOT / '.env'))
    write(ROOT / 'livekit.yaml', f'''port: 7880
bind_addresses:
  - 0.0.0.0
log_level: info
rtc:
  node_ip: {public_ip}
  tcp_port: 7881
  udp_port: 7882
keys:
  {lk_key}: {lk_secret}
turn:
  enabled: true
  udp_port: 3478
''', 0o640)
    run('chown', f'root:{USER}', str(ROOT / 'livekit.yaml'))
    write(Path('/etc/systemd/system/rikumo-livekit.service'), unit('Rikumo LiveKit', f'/usr/local/bin/livekit-server --config {ROOT}/livekit.yaml'))
    write(Path('/etc/systemd/system/rikumo-api.service'), unit('Rikumo API', f'{ROOT}/server/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1', str(ROOT / 'server')))

    webroot = Path('/var/www/rikumo-acme')
    webroot.mkdir(parents=True, exist_ok=True)
    site = Path('/etc/nginx/sites-available/rikumo-room')
    http_block = f'''server {{
    listen 80;
    server_name {domain};
    location /.well-known/acme-challenge/ {{ root {webroot}; }}
    location / {{ return 301 https://$host$request_uri; }}
}}
'''
    write(site, http_block)
    default_site = enabled / 'default'
    if default_site.is_symlink():
        default_site.unlink()
    elif default_site.exists():
        raise RuntimeError('Default Nginx site is not a symlink; refusing to remove it.')
    (enabled / 'rikumo-room').symlink_to(site)
    # Preserve SSH before enabling the local firewall; cloud security groups stay untouched.
    for rule in (f'{args.ssh_port}/tcp', '80/tcp', '443/tcp', '7881/tcp', '7882/udp', '3478/udp'):
        run('ufw', 'allow', rule)
    run('ufw', 'deny', '7880/tcp')
    run('ufw', 'deny', '8000/tcp')
    run('ufw', 'default', 'deny', 'incoming')
    run('ufw', 'default', 'allow', 'outgoing')
    run('ufw', '--force', 'enable')
    run('nginx', '-t')
    run('systemctl', 'enable', '--now', 'nginx')
    run('systemctl', 'reload', 'nginx')
    identifiers = ['-d', domain] if args.domain else ['--ip-address', str(public_ip), '--required-profile', 'shortlived']
    contact = ['--email', args.email] if args.email else ['--register-unsafely-without-email']
    run(CERTBOT, 'certonly', '--webroot', '-w', str(webroot), '--cert-name', domain,
        *identifiers, *contact, '--agree-tos', '--non-interactive')
    write(site, http_block + f'''server {{
    listen 443 ssl;
    server_name {domain};
    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    root {ROOT}/web/dist;
    location /api/ {{
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
    }}
    location /livekit/ {{
        proxy_pass http://127.0.0.1:7880/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }}
    location / {{ try_files $uri $uri/ /index.html; }}
}}
''')
    write(Path('/etc/letsencrypt/renewal-hooks/deploy/rikumo-nginx'), '#!/bin/sh\n/usr/sbin/nginx -t && /usr/bin/systemctl reload nginx\n', 0o755)
    write(Path('/etc/systemd/system/rikumo-cert-renew.service'), f'''[Unit]
Description=Renew Rikumo HTTPS certificate
After=network-online.target nginx.service
Wants=network-online.target
[Service]
Type=oneshot
ExecStart={CERTBOT} renew --cert-name {domain} --quiet
''')
    write(Path('/etc/systemd/system/rikumo-cert-renew.timer'), '''[Unit]
Description=Check Rikumo HTTPS certificate twice daily
[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=1800
Persistent=true
[Install]
WantedBy=timers.target
''')
    shutil.copy2(source / 'deploy/backup.py', ROOT / 'backup.py')
    write(Path('/etc/systemd/system/rikumo-backup.service'), f'''[Unit]
Description=Rikumo SQLite backup
[Service]
Type=oneshot
User={USER}
Group={USER}
UMask=0077
ExecStart=/usr/bin/python3 {ROOT}/backup.py
''')
    write(Path('/etc/systemd/system/rikumo-backup.timer'), '''[Unit]
Description=Daily Rikumo database backup
[Timer]
OnCalendar=*-*-* 05:00:00 Asia/Shanghai
Persistent=true
[Install]
WantedBy=timers.target
''')
    run('nginx', '-t')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'rikumo-livekit', 'rikumo-api', 'rikumo-backup.timer', 'rikumo-cert-renew.timer')
    run('systemctl', 'reload', 'nginx')
    print(f'安装步骤完成。访问 https://{domain}/，使用设定的超级管理员账号登录。')
    print('云平台安全组仍需放行上面列出的端口；不要开放 7880、8000。')
    print('此版本提供 UDP 和 ICE/TCP 连接，尚未配置 TURN/TLS 443 回退。')
    print('部署后的首次登录、外网双向通话与备份恢复还需验收。')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'部署中止：{error}\n已有数据不会自动删除；请保留现场排查，勿反复重装。', file=sys.stderr)
        sys.exit(1)
