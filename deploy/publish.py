#!/usr/bin/env python3
"""Package, upload and initially deploy through the operating system's OpenSSH client."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import uuid

PROJECT = Path(__file__).resolve().parents[1]


def stop_local_services() -> None:
    if os.name != 'nt':
        return
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if not shell:
        raise RuntimeError('找不到 PowerShell，无法自动停止本地项目。')
    print('构建前自动停止本项目的本地开发服务，释放前端依赖文件。', flush=True)
    subprocess.run([
        shell, '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', str(PROJECT / 'stop-dev.ps1'),
    ], cwd=PROJECT, check=True)


def password_environment(host: str, user: str, port: int, save: bool) -> dict[str, str] | None:
    config_path = PROJECT / '.dev/ssh-password.json'
    if save:
        password = getpass.getpass('SSH 密码（仅保存到本机私有配置，输入不显示）：')
        if not password or '\n' in password or '\r' in password:
            raise ValueError('SSH 密码不能为空或包含换行。')
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({
            'host': host, 'user': user, 'port': port, 'password': password,
        }, ensure_ascii=False), encoding='utf-8')
        config_path.chmod(0o600)
        print('SSH 密码已保存到本机 .dev/ssh-password.json，不会加入发布包。', flush=True)
    if not config_path.exists():
        return None
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('本机 SSH 密码配置格式不正确。')
    if (config.get('host'), config.get('user'), config.get('port')) != (host, user, port):
        raise ValueError('本机 SSH 密码配置与服务器不匹配，请使用 --save-ssh-password 重新设置。')
    password = config.get('password')
    if not isinstance(password, str) or not password or '\n' in password or '\r' in password:
        raise ValueError('本机 SSH 密码配置无效，请重新设置。')
    helper = PROJECT / 'deploy/ssh_askpass.py'
    if os.name == 'nt':
        launcher = PROJECT / '.dev/ssh-askpass.cmd'
        if '%' in str(PROJECT) or '%' in sys.executable:
            raise ValueError('自动密码登录暂不支持路径包含百分号。')
        launcher.write_text(f'@"{sys.executable}" "{helper}"\r\n', encoding='utf-8')
    else:
        launcher = PROJECT / '.dev/ssh-askpass.sh'
        launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(helper))}\n', encoding='utf-8')
        launcher.chmod(0o700)
    environment = os.environ.copy()
    environment.update({
        'SSH_ASKPASS': str(launcher),
        'SSH_ASKPASS_REQUIRE': 'force',
        'RIKUMO_SSH_PASSWORD_FILE': str(config_path),
    })
    environment.setdefault('DISPLAY', 'rikumo-askpass')
    return environment


def release_files(include_dist: bool = False) -> list[Path]:
    # Allowlist: development databases, credentials, certificates and dependency caches
    # are not part of the release. Reject symlinks rather than follow them outside source.
    result = [PROJECT / relative for relative in (
        'server/pyproject.toml', 'web/package.json', 'web/package-lock.json',
        'web/tsconfig.json', 'web/index.html', 'web/vite.config.ts',
        'deploy/deploy.py', 'deploy/update.py', 'deploy/backup.py', 'deploy/README.md',
    )]
    directories = ['server/app', 'web/src', 'web/public']
    if include_dist:
        directories.append('web/dist')
        if not (PROJECT / 'web/dist/index.html').is_file():
            raise RuntimeError('Local frontend build did not produce dist/index.html.')
    for relative in directories:
        root = PROJECT / relative
        if root.is_symlink():
            raise RuntimeError(f'Symlink is not permitted in release: {relative}')
        if not root.exists():
            if relative == 'web/public':
                continue
            raise RuntimeError(f'Missing source directory: {relative}')
        for path in root.rglob('*'):
            if path.is_symlink():
                raise RuntimeError(f'Symlink is not permitted in release: {path.relative_to(PROJECT)}')
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo'):
                if path.name == '.env' or path.name.startswith('.env.'):
                    continue
                result.append(path)
    for path in result:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f'Missing or unsupported release file: {path.relative_to(PROJECT)}')
    return sorted(set(result))


def main() -> None:
    parser = argparse.ArgumentParser(description='One-command initial publish to a fresh Ubuntu 24.04 server.')
    parser.add_argument('--host', required=True, help='Public server IPv4')
    parser.add_argument('--user', default='root', help='root or sudo-capable SSH user')
    parser.add_argument('--port', type=int, default=22)
    parser.add_argument('--identity', type=Path, help='Optional SSH private key path; never packaged')
    parser.add_argument('--domain', help='Optional domain; default is IP HTTPS')
    parser.add_argument('--email', help='Optional certificate contact email')
    parser.add_argument('--admin-name', default='璃雲')
    parser.add_argument('--package-only', action='store_true', help='Create release archive without contacting server')
    parser.add_argument('--upload-only', action='store_true', help='Upload and extract, without installing services')
    parser.add_argument('--update', action='store_true', help='Update existing installation instead of initial install')
    parser.add_argument('--web-only', action='store_true', help='With --update, publish frontend only')
    parser.add_argument('--save-ssh-password', action='store_true', help='Prompt once and save SSH password locally for automatic login')
    args = parser.parse_args()
    if args.web_only and not args.update:
        parser.error('--web-only requires --update.')
    host = str(ipaddress.IPv4Address(args.host))
    if not ipaddress.IPv4Address(host).is_global:
        parser.error('--host must be a public IPv4 address.')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', args.user) or not 1 <= args.port <= 65535:
        parser.error('Invalid SSH user or port.')
    if args.identity and not args.identity.is_file():
        parser.error('SSH private key file not found.')
    if args.identity and args.save_ssh_password:
        parser.error('--identity cannot be combined with --save-ssh-password.')
    auth_env = None if args.identity or args.package_only else password_environment(host, args.user, args.port, args.save_ssh_password)
    if args.update:
        npm = shutil.which('npm')
        if not npm:
            parser.error('Local Node.js/npm is required to build the frontend before upload.')
        stop_local_services()
        print('在本机安装前端依赖并构建网页；成功后再上传，服务器不下载 npm 依赖。', flush=True)
        subprocess.run([npm, 'ci'], cwd=PROJECT / 'web', check=True)
        subprocess.run([npm, 'run', 'build'], cwd=PROJECT / 'web', check=True)
    token = uuid.uuid4().hex
    name = f'rikumo-release-{token}'
    output = PROJECT / '.dev/releases'
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / f'{name}.tar.gz'
    files = release_files(include_dist=args.update)
    with tarfile.open(bundle, 'w:gz') as archive:
        for path in files:
            entry = archive.gettarinfo(str(path), arcname=path.relative_to(PROJECT).as_posix())
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ''
            entry.mode = 0o644
            with path.open('rb') as content:
                archive.addfile(entry, content)
    with bundle.open('rb') as content:
        digest = hashlib.file_digest(content, 'sha256').hexdigest()
    print(f'发布包：{bundle}\n源文件数：{len(files)}\nSHA256：{digest}', flush=True)
    if args.package_only:
        return
    ssh = shutil.which('ssh')
    scp = shutil.which('scp')
    if not ssh or not scp:
        parser.error('Install the operating system OpenSSH client (ssh and scp) first.')
    key_args = ['-i', str(args.identity.resolve())] if args.identity else []
    # Keep normal host-key verification. New hosts ask for explicit fingerprint confirmation.
    options = ['-o', 'StrictHostKeyChecking=yes' if auth_env else 'StrictHostKeyChecking=ask', '-o', 'ConnectTimeout=20', *key_args]
    if auth_env:
        options += ['-o', 'PreferredAuthentications=password', '-o', 'NumberOfPasswordPrompts=1']
    target = f'{args.user}@{host}'
    remote_bundle = f'{name}.tar.gz'
    print('开始上传；使用本机已保存的 SSH 密码自动登录。主机必须已通过指纹验证。' if auth_env
          else '开始上传；密码由 SSH 输入提示读取。首次连接需核对服务器指纹。', flush=True)
    subprocess.run([scp, '-P', str(args.port), *options, str(bundle), f'{target}:{remote_bundle}'], check=True, env=auth_env)
    # Paths are generated ASCII names inside the SSH user's home. No credential appears in commands.
    remote_setup = (
        f'set -eu; cd "$HOME"; '
        f'printf "%s\\n" {shlex.quote(digest + "  " + remote_bundle)} | sha256sum -c -; '
        f'mkdir {shlex.quote(name)}; '
        f'tar --no-same-owner --no-same-permissions -xzf {shlex.quote(remote_bundle)} -C {shlex.quote(name)}; '
        f'cd {shlex.quote(name)}; '
    )
    if args.upload_only:
        remote_setup += 'printf "上传完成，源文件目录：%s\\n" "$PWD"'
    else:
        deploy_args = ['python3', 'deploy/update.py'] if args.update else ['python3', 'deploy/deploy.py', '--public-ip', host, '--ssh-port', str(args.port), '--admin-name', args.admin_name]
        if args.update:
            deploy_args += ['--prebuilt-web']
        if args.web_only:
            deploy_args += ['--web-only']
        if args.domain and not args.update:
            deploy_args += ['--domain', args.domain]
        if args.email and not args.update:
            deploy_args += ['--email', args.email]
        if args.user != 'root':
            deploy_args.insert(0, 'sudo')
        remote_setup += shlex.join(deploy_args)
    subprocess.run([ssh, '-t', '-p', str(args.port), *options, target, remote_setup], check=True, env=auth_env)
    print('上传流程完成。源文件和发布包留在服务器登录用户目录，便于排查。')
    if not args.upload_only:
        print(f'安装命令已完成；请访问 https://{args.domain or host}/ 验收登录与外网通话。')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n发布已被用户中断；请保留发布目录，确认当前阶段后再重试。', file=sys.stderr)
        sys.exit(130)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f'发布中止：{error}\n不会自动删除服务器现场。修复问题后再安排后续步骤。', file=sys.stderr)
        sys.exit(1)
