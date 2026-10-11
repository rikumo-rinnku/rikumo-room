# 璃雲の部屋（Rikumo Room）

面向小团队的网页语音平台，使用 Vue 3 + TypeScript + Vite、Python / FastAPI、SQLite 和 LiveKit SFU。支持自行部署，无需 Docker。

目前处于团队测试阶段，建议先用于测试。自定义音频处理路径仍在排查电音、断续和声音加速问题；受影响设备可使用“原始麦克风直通”。

## 许可证

本项目源码使用 [MIT License](LICENSE)。RNNoise 及其 WASM 包保留各自的 BSD / Apache-2.0 许可证，声明见 [第三方许可说明](web/public/licenses/NOTICE.txt)。

## 本地开发

需要 Python 3.12+、Node.js 20+ 和 LiveKit Server。项目直接运行本机进程，不需要 Docker。

1. 运行 `./install-livekit.ps1` 获取官方最新 Windows 版 LiveKit Server（也可以从 [LiveKit Server Releases](https://github.com/livekit/livekit/releases) 手动下载）。
2. 首次安装依赖：`py -3.12 -m venv server/.venv`、`server/.venv/Scripts/python.exe -m pip install -e server`、`npm --prefix web install`。
3. 复制 `.env.example` 为 `.env`，按需修改超级管理员账号和密码。
4. 在仓库根目录运行 `./start-dev.ps1`，打开 `http://localhost:5173/`。运行 `./stop-dev.ps1` 可停止本地服务。日志和进程记录保存在 `.dev`。

也可以在三个终端分别运行 `livekit-server --dev --bind 0.0.0.0`、`server/.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000` 和 `npm --prefix web run dev -- --host 0.0.0.0`。

## 手机访问本地开发页面

手机与电脑连接同一 WiFi。局域网语音测试需要可信 HTTPS：

1. 在项目目录运行 `./setup-lan-test.ps1 -LocalAddress 192.168.50.85`，生成本地测试证书及局域网配置。需要 PATH 中有 OpenSSL；证书与私钥保存在被忽略的 `.dev/tls`，私钥不要分享。
2. 运行 `./stop-dev.ps1`、`./start-dev.ps1` 重启服务。网页改为 `https://192.168.50.85:5173/`，开发环境语音信令通过同一地址的 `/livekit` 代理，LiveKit 媒体使用指定的 WiFi IP。
3. 在**管理员 PowerShell** 中运行 `./allow-lan-test.ps1`。它放行网页/证书下载 TCP 5173、5174，语音 TCP 7881、UDP 7882；规则限定程序、WiFi 接口和局域网子网。曾运行过旧版脚本也需重新运行。
4. 安卓手机打开 `http://192.168.50.85:5174/` 下载公开测试证书；在系统设置中搜索“安装证书”，选择 **CA 证书**并安装下载的文件。
5. 手机打开 `https://192.168.50.85:5173/`，确认无证书错误，登录后加入房间并允许麦克风权限。手机与电脑同时测试请使用不同账号。

电脑浏览器也需将 `.dev/tls/ca.cer` 安装到当前用户的“受信任的根证书颁发机构”。CA 有效期一年，网页证书有效期 30 天，运行设置脚本可重新签发网页证书。IP 改变后重新设置并重启；防火墙脚本默认 IP `192.168.50.85`、子网 `192.168.50.0/24`、接口 `WLAN`，可通过参数修改。此证书用于本地测试，正式部署使用可信域名证书。

## Ubuntu 服务器部署

Python 一键首次部署脚本及准备事项见 [deploy/README.md](deploy/README.md)。本机运行 `python deploy/publish.py --host 公网IP --user root --port 22` 自动打包、SSH 上传并安装。支持新建 Ubuntu 24.04 服务器，原生服务运行，无需 Docker；自动生成独立密钥、配置 IP 或域名 HTTPS、本机防火墙、开机自启和每日 SQLite 备份。已用于团队外网测试；不同网络及设备仍需自行验证。

## 当前范围

- 管理员与游客账号、平台级每日邀请链接、房间列表、房间加入/切换/退出、实时语音。
- 游客每日按 Asia/Shanghai 时间凌晨 4 点清理；当时正在通话的游客在离房或服务端确认断开后清理。
- 管理员房间管理、成员禁言/踢出/封禁；超级管理员的管理员邀请码和密码重置。
- 通话底部常驻音频控制：麦克风输入音量 0–200%、总收听 0–200%，独立开关及本地提示音；成员卡片可单独调节对方收听音量。进入设置不隐藏音频控制，全局音量保存在当前浏览器。
- 网络中断保留登录并尝试语音重连，按原因提示踢出/房间关闭/重复账号或连接失败；播放被浏览器拦截时可手动恢复收听。设备列表随拔插更新，麦克风断开后需重新开麦，扬声器断开尝试系统默认输出，设备切换失败还原选择。
- 设置页集中管理个人资料、账号和邀请；管理员及超级管理员均可查看访客所在房间、踢出、拉黑和解除拉黑，超级管理员还可为普通管理员设置新密码。
- 超级管理员日志页面默认重要操作，用中文展示，支持显示全部、操作类型、时间、操作人、对象、结果及关键词筛选，每页 30 条。原始接口信息可展开查看。
- 可选回声消除、自动增益、浏览器原生降噪、RNNoise、收音阈值及输入音量处理；提供原始麦克风直通。
- 通话诊断，以及管理员主动开启的最近三分钟分成员语音缓存与导出。开启后向房间成员显示提醒，录音在开启者浏览器保存；详见 [问题记录说明](docs/problem-recording.md)。
- 暂不包含文字聊天和屏幕共享。

## 已知限制

- 自定义音频处理可能在部分设备上出现电音、断续或声音加速，目前尚未确认唯一根因。原始直通在团队对照中持续正常，但不保证所有设备都相同。
- 问题记录的会话状态保存在后端单进程内存中；暂不支持多个 API worker 共享状态。
- 当前部署脚本未配置 TURN/TLS 443 回退；部分受限网络可能无法建立语音连接。
- 本项目面向小团队，尚未完成大规模负载与公开互联网安全审计。

后端管理功能的回归测试：在 `server` 目录运行 `.venv/Scripts/python.exe -m unittest discover -s tests -v`。测试使用独立临时数据库，不修改开发账号或现有日志。

普通用户的邀请链接使用 `https://域名/{邀请码}` 路径；域名根路径只显示管理员登录。正式部署的静态站点需把单层邀请路径回退到 `index.html`。本地 LiveKit 使用开发模式密钥，仅供开发。正式部署时可将 LiveKit 和 FastAPI 配置为原生系统服务，并由反向代理提供 HTTPS；需换成独立密钥和生产配置，并开放 WebRTC 网络端口。
