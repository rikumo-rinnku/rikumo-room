# Ubuntu 原生一键部署

使用 Python 标准库编排安装，不依赖 Docker。目标是新建、专用的 Ubuntu 24.04 x64/ARM64 服务器，首次部署；不自动覆盖已有安装，也不自动迁移本机数据库。

## 需要准备

- 公网 IPv4、Ubuntu 版本及 CPU 架构。
- SSH 用户和端口；可通过 sudo 获得 root 权限。
- 测试可以直接使用公网 IP：不传 `--domain`，脚本申请 Let's Encrypt 可信 IP 证书，不需要解析域名或给手机安装 CA。证书有效期约 6 天，因此 TCP 80 必须持续可达，脚本每天检查两次续期。商家对 IP 网页入口的接入限制仍需按其规定确认，HTTPS 不替代备案要求。
- 如果使用域名，A 记录指向服务器；若有 AAAA 记录，IPv6 也必须正确可达，否则先去掉 AAAA。国内网页入口的备案及接入要求需向服务器商确认。
- 证书联系邮箱可通过 `--email` 提供，也可省略；省略时不会收到账号邮件提醒。服务器可访问 Ubuntu 软件源、PyPI、npm 和 GitHub。
- 云平台安全组允许 TCP：SSH 端口、80、443、7881；UDP：7882、3478。API 8000 和 LiveKit 信令 7880 不对公网开放。
- 是否迁移现有管理员、房间和日志。默认新建数据库；需要迁移时先制定迁移步骤再安装。

将项目上传到服务器，如 `/srv/rikumo-source`，排除 `.env`、`.dev`、`server/data`、虚拟环境和 `node_modules`。不需要提供 SSH 密码、私钥或云平台密钥给聊天；超级管理员密码在服务器终端输入。

## 执行

### 在电脑上自动打包、上传并安装

在 Windows PowerShell 中打开本机项目目录，执行（需要 Python 3.11+ 和系统 OpenSSH 客户端）：

```powershell
cd E:\projects\rikumo-room
python deploy/publish.py --host 203.0.113.10 --user root --port 22
```

脚本自动创建 `.dev/releases` 发布包，通过 `scp` 上传到登录用户目录，经 SHA256 校验后解压，再通过 SSH 调用下面的服务器部署命令。上传时密码由 OpenSSH 交互读取；首次连接保留主机指纹校验。SSH 密码可能在上传和远程执行时分别提示输入。超级管理员密码在远程安装时输入，独立于 SSH 密码。

这是**首次部署**，默认新建数据库，原电脑的账号、房间和日志不会上传。上传包只包含应用源代码、构建配置和部署文件；不包含 `.env`、本地数据库、私钥、测试证书、虚拟环境或 `node_modules`。服务器上的运行密钥由部署脚本生成。

可加 `--package-only` 只打包，或 `--upload-only` 上传解压而不安装。使用 SSH 密钥时增加 `--identity 私钥文件路径`；该私钥不会加入发布包。非 root 登录可指定 `--user 用户名`，需要 sudo 权限。失败时保留服务器目录，初次安装脚本拒绝覆盖已有安装，需要按报错排查而非删除数据后反复执行。

### 后续更新

自动输入 SSH 密码：首次在原发布命令后加 `--save-ssh-password`，输入一次密码，保存到本机 `.dev/ssh-password.json`。之后相同服务器、用户和端口的发布自动读取该配置，用 OpenSSH ASKPASS 完成登录；密码不进入命令参数或发布包。该文件以明文保存在本机，不要分享；已有 `.gitignore` 排除整个 `.dev`。更换密码时再次加该参数。使用 `--identity` 时继续走密钥登录。自动登录保留严格主机指纹校验；首次连接新服务器需先手动 `ssh` 核对并确认指纹。此功能仅自动填写 SSH 密码，首次安装中的管理员密码及非 root 用户的 sudo 密码仍需交互输入。自动登录尚待实际发布验证。

```powershell
python deploy/publish.py --host 203.0.113.10 --user root --port 22 --update --web-only --save-ssh-password
```

2026-10-03：页面业务报错、表单校验和常见语音/设备错误使用中文提示。此修改同时涉及前后端，发布时使用完整 `--update`，不要加 `--web-only`；本地代码修改不会自动同步服务器。

更新前后端应用代码：

```powershell
python deploy/publish.py --host 203.0.113.10 --user root --port 22 --update
```

本次收听音量 200% 修改仅涉及网页，使用：

```powershell
python deploy/publish.py --host 203.0.113.10 --user root --port 22 --update --web-only
```

更新入口是 `deploy/update.py`。本机发布脚本在电脑上运行 `npm ci` 和 `npm run build`，将生成的 `web/dist` 一并上传，远程自动使用 `--prebuilt-web`，服务器无需下载 npm 依赖；本机需安装 Node.js/npm。独立发布目录准备失败时不切换当前版本；切换前使用 SQLite 在线备份。完整更新短暂停止 API 并切换代码和 Python 虚拟环境，等待健康接口就绪后更新网页；仅网页更新不重启 API。两种更新均不重启 LiveKit，不重新签发证书，不修改 UFW、密钥和超级管理员配置。完整更新仍需服务器安装 Python 依赖。

Windows 更新在 `npm ci` 前自动执行 `stop-dev.ps1`，停止通过本项目启动脚本记录的开发服务，以及其前端依赖目录中的 esbuild 子进程，避免文件占用。通过路径和启动时间核对进程，记录不匹配时中止发布；手动启动且没有项目记录的服务仍需自行关闭。发布完成后本地服务保持关闭，可运行 `start-dev.ps1` 重新启动。`KeyboardInterrupt` 表示发布被中断，不能据此认定文件占用。

切换失败时自动恢复上一版代码及 Python 环境；数据库不自动恢复，以避免丢失更新后的用户操作。若后续引入不兼容的数据库迁移，需要单独制定迁移与数据库回退流程，不能把此入口当作任意模式迁移工具。当前服务依赖单 API worker 的每日重置调度，更新保持这个约定。

新旧发布目录保留在 `/opt/rikumo-room/.releases`，数据库更新快照保存在 `/var/lib/rikumo-room/update-backups`，暂不自动删除；长期更新需安排清理和异地备份。该入口不更新 LiveKit 可执行文件、系统包或 Node.js，组件升级需另外安排。浏览器刷新后使用新页面；正在通话者刷新会结束当前页面的通话，建议在空闲时刷新。

### 已手动上传项目时，在服务器上安装

本次 IP 测试部署命令（端口 22）：

```bash
sudo python3 deploy/deploy.py --public-ip 203.0.113.10 --ssh-port 22
```

安装后访问 `https://203.0.113.10/`。可追加 `--plan` 先查看计划。脚本会在 root 拥有的独立虚拟环境安装 Certbot 5.4 以上版本，使用 IP 证书和 webroot 校验；默认新建账号和数据库，不迁移本机数据。证书签发实际成功前，不应把 HTTP 或浏览器跳过证书错误当作完整的语音测试入口。

域名部署方式：

先查看计划（不修改系统）：

```bash
python3 deploy/deploy.py --domain voice.example.com --public-ip 你的公网IP --email admin@example.com --plan
```

首次安装：

```bash
sudo python3 deploy/deploy.py --domain voice.example.com --public-ip 你的公网IP --email admin@example.com --admin-name 璃雲 --ssh-port 22
```

命令应在上传的项目根目录运行。脚本安装系统依赖、固定版本 LiveKit（默认与本机一致的 1.13.7，并校验官方发布摘要）、Python 虚拟环境，构建网页，生成独立密钥，配置 Nginx 同源 API/WebSocket、Let's Encrypt 证书及续期、systemd 开机自启和失败重启。

脚本先放行指定 SSH 端口，再配置并启用 UFW 本机防火墙：默认拒绝入站，仅开放网页及语音所需端口，禁止公网访问 7880 和 8000；不修改 SSH 设置或云平台安全组。此脚本用于专用新服务器，若已有其他业务请先安排规则合并。云平台防火墙若已全部放行，可保持不变。仅在可信官方域名和发行包上下载依赖。失败后不会自动删除安装目录，保留现场排查；该脚本暂不是更新/恢复工具。

## 运维

```bash
sudo systemctl status rikumo-api rikumo-livekit nginx
sudo journalctl -u rikumo-api -u rikumo-livekit --since today
sudo systemctl restart rikumo-api rikumo-livekit
sudo systemctl start rikumo-backup
```

数据库位于 `/var/lib/rikumo-room/rikumo-room.sqlite3`；每天北京时间 05:00 使用 SQLite 在线备份，保留最近 14 份成功快照，目录 `/var/lib/rikumo-room/backups`。这是本机备份，需要另外下载或复制到异地，避免整机故障丢失。恢复时停止 API，保存原数据库及 WAL/SHM 文件，再恢复快照并检查所有权后启动；不要在 API 运行时替换数据库。

服务以普通系统用户运行；配置包含初始化密码和独立密钥，保存在 `/opt/rikumo-room/.env`，只允许 root 和服务用户读取。更改该文件中的初始化密码不会重置数据库已有账号。

当前提供直连 UDP、ICE/TCP 和 TURN/UDP；企业网络等需要 TURN/TLS 443 的场景尚未配置，需独立设计入口与证书。HTTPS 证书签发、域名可达、服务启动和真实外网通话须在目标服务器验收，Windows 本地无法完成这些验收。

IP 证书官方说明：https://letsencrypt.org/2026/03/11/shorter-certs-certbot/
