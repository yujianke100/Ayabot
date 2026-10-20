<p align="center">
  <img src="https://raw.githubusercontent.com/yujianke100/Ayabot/main/figs/logo.png" alt="ayabot" width="600" />
</p>

# Ayabot：免费开源的 Bilibili 直播间机器人

<p align="center">
  <a href="https://github.com/yujianke100/ayabot/releases"><img src="https://img.shields.io/github/v/release/yujianke100/ayabot" alt="Release"></a>
  <a href="https://github.com/yujianke100/ayabot/pkgs/container/ayabot"><img src="https://img.shields.io/badge/docker-ghcr.io-blue?logo=github" alt="ghcr.io"></a>
  <a href="https://hub.docker.com/r/yujianke100/ayabot"><img src="https://img.shields.io/badge/docker-hub-blue?logo=docker" alt="Docker Hub"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-GPLv3-blue" alt="License"></a>
</p>

**完全免费**的超轻量 B 站直播间弹幕机器人。签到抽签、盲盒统计、AI 对话、自动欢迎/感谢、定时消息、Web 后台管理，一应俱全。
开箱即用，小白友好。纯 Python 跨平台，无需 systemd / sudo。

🌐 **产品主页（国内可直连）**：<https://ayabot.jianke-yu.online>　·　版本更新日志详见 [GitHub Releases](https://github.com/yujianke100/ayabot/releases)。

---

## ✨ 核心优势

| 优势 | 说明 |
|------|------|
| 🆓 **完全免费** | 本项目 **100% 免费开源**（GPLv3），功能无任何限制。市面同类 Bot 动辄月付数十元，Ayabot 分文不取 |
| 🪶 **极致轻量** | **百兆内存即可运行**，树莓派、低配云服务器都能轻松跑。无数据库依赖，无需 Redis / MySQL |
| 🖥️ **全平台覆盖** | **Windows** 提供单文件 .exe 桌面软件（托盘图标 + 日志查看器）；**macOS / Linux** 可直接源码运行或 **Docker 一键部署**；ARM 架构（树莓派、苹果 M 系列）完美支持 |

---

## 初始账号

| 用户名 | 密码 | 说明 |
|--------|------|------|
| `ayabot` | `123456` | 管理员账号，首次登录强制修改密码 |

> 密码忘记或想重置？运行 `python -m app.reset_admin` 即可重置为随机密码。
> Windows .exe 版：右键托盘图标 → **重置管理员密码**（重置为 123456）。

---

## 🚀 快速上手

### 🪟 Windows 用户（推荐）

下载 `Ayabot.exe` 后直接双击运行，无需 Python 环境：

> **下载链接**：[Ayabot-latest.exe](https://github.com/yujianke100/ayabot/releases/latest/download/Ayabot-latest.exe)（永远指向最新版）
>
> 🇨🇳 **国内镜像**：[gh-proxy.com 加速](https://gh-proxy.com/https://github.com/yujianke100/ayabot/releases/latest/download/Ayabot-latest.exe)
> （管理后台里点「检查更新」会自动测速并推荐当前可用的线路，不用自己猜）

```
Ayabot.exe 单独一个文件，双击即运行
├── 托盘图标（右下角）
│   ├── 🌐 打开管理后台
│   ├── 🔌 设置端口
│   ├── 🔑 重置管理员密码
│   ├── 📋 查看日志（级别筛选）
│   └── ❌ 退出
├── 端口管理（持久化，默认 19810）
├── 房间状态持久化（退出时保存运行中的房间，启动自动恢复）
└── 配置和数据 → %LOCALAPPDATA%\Ayabot\
```

> 首次启动自动从 exe 中释放默认配置。浏览器打开 `http://localhost:19810`。

> **⚠️ 第一次双击提示「无法验证发布者，你确定要运行此软件吗？」**
> 这是 Windows 对「从网上下载、没有数字签名」的 exe 的例行提示，点 **「运行」** 即可。
> 程序启动后会自动清掉这个下载标记，**之后启动不再提示**（也可手动：右键 exe → 属性 → 勾选「解除锁定」→ 确定）。

### 🐳 用 Docker 跑（小白首选，无需 Python）
镜像支持 **AMD64（普通 PC / 云服务器）和 ARM64（树莓派、苹果 M 系列、Oracle ARM 云）**：

```bash
# GitHub Container Registry（主镜像）
docker pull ghcr.io/yujianke100/ayabot:latest

# Docker Hub（备用）
docker pull yujianke100/ayabot:latest
```

> **🇨🇳 国内加速**：ghcr.io 国内拉取慢？用镜像站：`docker pull docker.mirrors.ustc.edu.cn/ghcr.io/yujianke100/ayabot:latest`
> 更多可用镜像站可查 → [demo.kentxxq.com/app/mirror](https://demo.kentxxq.com/app/mirror)
```bash
mkdir ayabot && cd ayabot
wget https://raw.githubusercontent.com/yujianke100/ayabot/main/config.example.yaml -O config.yaml
wget https://raw.githubusercontent.com/yujianke100/ayabot/main/docker-compose.yml -O docker-compose.yml
# 编辑 config.yaml，填直播间号和主播 UID
mkdir -p data rooms accounts
docker compose up -d
```

浏览器打开 `http://localhost:19810` 进入 Web 后台，在「B站账号」扫码登录，然后在「房间管理」中创建房间、启动机器人。

> **改端口？** 编辑 `docker-compose.yml`，修改 `ports` 左侧值和 `AYABOT_PORT` 环境变量。
> 例如 Nginx 反向代理用 8000 → 容器内 19810：`"8000:19810"` + `AYABOT_PORT=19810`

### 🐍 用 Python 直接跑（开发/自定义）

```bash
git clone https://github.com/yujianke100/ayabot.git
cd ayabot
pip install -r requirements.txt
cp config.example.yaml config.yaml
# 编辑 config.yaml：填直播间号和主播 UID

# 启动 Web 管理后台（内置 Bot 进程管理器）
python web_serve.py
```

或用启动脚本一键启动（自动激活虚拟环境）：
```bash
bash start.sh
```

浏览器打开 `http://localhost:19810` 进入 Web 后台，操作同上。

> **端口被占用了？** `python web_serve.py --port 你想要的端口号` 或 `AYABOT_PORT=你想要的端口号 python web_serve.py`

---

## 🛠️ 命令行工具

### 重置管理员密码

```bash
# 重置为随机密码（自动生成并打印）
python -m app.reset_admin

# 重置为指定密码
python -m app.reset_admin --password mypass

# 重置指定用户
python -m app.reset_admin --username ayabot --password mypass

# 重置但不强制首次改密码
python -m app.reset_admin --no-reset-flag
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AYABOT_PORT` | `19810` | Web 管理后台监听端口。**启动前设置**，避免端口冲突进不去后台 |

---

## 🎯 直播间弹幕命令

在直播间发以下指令，机器人自动回复：

| 发这个 | 机器人会 |
|--------|----------|
| `#签到` | 签到 + 显示连续天数 + 排名 |
| `#抽签` | 今日运势 |
| `#今日盲盒[:用户名]` | 统计自己或指定用户今日盲盒 |
| `#本月盲盒[:用户名]` | 本月盲盒汇总 |
| `#ayabot <说点啥>` | AI 聊天（唤醒词可在后台修改） |
| `#帮助` | 列出所有命令 |
| 点赞直播间 | 自动感谢点赞（后台可开关） |
| 分享直播间 | 自动感谢分享（后台可开关） |
| 关注主播 | 自动感谢关注（后台可开关） |

> 中文 `#` 和英文 `#` 都行，`＃签到` 也能识别。
> 开启「免#指令」后，不带 `#` 前缀也能触发（如直接发「签到」）。
> 开启「AI免#前缀唤醒」后，弹幕以唤醒词开头即触发 AI 回复。

---

## ⚙️ Web 管理后台

浏览器打开 `http://你的IP:19810` 进入后台（初始账号 `ayabot` / `123456`，首次登录强制修改）。

> 首次登录后可在右上角修改用户名和密码，状态持久化，重启不丢失。

### 🚀 新手四步上手

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | **登录 B站 账号** | 「用户管理」→「B站账号」→「扫码登录」，用 B站 App 扫描二维码 |
| 2 | **新建直播间** | 「房间管理」→「新建房间」，填写主播 UID 和直播间号，关联已登录的 B站 账号 |
| 3 | **配置并启动** | 创建后在房间详情中配置功能开关、AI 回复等，点击「启动」| 
| 4 | **创建主播账号（可选）** | 公网部署或帮别人搭建时，新建「普通用户」账号并授权对应房间，对方登录后仅可见自己房间的数据 |

### 页面功能

| 页面 | 能干嘛 |
|------|--------|
| 📊 **送礼排行** | 按日期看谁送了多少礼物/盲盒，带柱状图 |
| 🎨 **精美导出** | 导出送礼明细卡片，支持 PNG 导出（所见即所得）| 
| 🤖 **AI 回复** | 开关、改唤醒词、配 API Key、调温度、改人设。支持三种触发方式 |
| ⚙️ **机器人配置** | 功能开关、冷却时间、弹幕限流、欢迎/感谢/盲盒模板、PK汇报、点赞/转发/关注感谢、定时消息、关键词回复、签文 |
| 👥 **用户管理** | 管理员增删改查、B站账号登录/验证、主播账号授权 |
| 💬 **弹幕记录** | 查看直播间历史弹幕 |
| 🗑️ **数据管理** | 清理旧数据 |

### 功能开关

| 功能 | 说明 |
|------|------|
| 免#指令 | 不带 `#` 也能触发签到、盲盒等指令 |
| AI免#前缀唤醒 | 弹幕以唤醒词开头即触发 AI 回复 |
| 包含关键词触AI | 弹幕任何位置含唤醒词即触发 AI 回复（需开启 AI免#前缀唤醒） |
| 全局数字转大写（反屏蔽） | 所有回复中的数字转为大写中文，避开数字拦截 |
| 点赞/转发/关注感谢 | 自动感谢观众的点赞、分享直播间、关注行为 |
| PK汇报 | PK 开始时汇报对手粉丝数、大航海、观众数、贡献值 |
| 弹幕记录 | 开启后记录所有弹幕到数据库，可在 Web 查看 |
| 大航海欢迎/感谢 | 舰长/提督/总督专属模板 |
| 盲盒统计（今日/本月） | 区分今日和本月的盲盒统计，支持自定义回复文本 |
| 盲盒数字转中文 | 盲盒统计中的数字转为中文（全局未开启时可用） |
| 玻璃心模式 | 盲盒亏损时隐藏真实收益，回复「服务器繁忙」 |
| 自定义签文 | 修改抽签结果的六种签文内容 |

### 关键词回复

支持：
- **包含匹配** / **精确匹配**
- 可限制仅特定 UID 触发
- 可配置冷却时间
- **模态框编辑**，与 UID 特定欢迎模板风格一致

---

## 📝 配置说明

```yaml
room_display_id: 1946287911        # ← 改成你的直播间号
anchor_uid: 1000000                # ← 改成主播的 UID
```

> 用户名密码不写在配置里，初始密码见上方表格，也可通过 `python -m app.reset_admin` 随时重置。
> 所有配置均可通过 Web 后台修改，无需手动编辑 YAML。

完整的配置示例见 [config.example.yaml](config.example.yaml)。

---

## 💾 数据与进程管理

| 文件/目录 | 说明 |
|-----------|------|
| `rooms/<id>/data/bot.db` | 房间独立 SQLite 数据库 |
| `rooms/<id>/bot.pid` | Bot 进程 PID 文件 |
| `rooms/<id>/bot.log` | Bot 运行日志 |
| `rooms/<id>/config.yaml` | 房间专属配置 |
| `accounts/<uid>/credential.json` | B 站扫码登录凭据 |
| `data/users.json` | WebUI 用户账号 |
| `data/templates.json` | 预设模板 |

### 跨平台进程管理

纯 Python 实现（`app/process_manager.py`），自动适配运行模式：

- **普通 Python 环境**：`subprocess.Popen` 子进程管理 Bot
- **PyInstaller 冻结模式**（.exe）：同进程 asyncio Task 运行 Bot（无需子进程）
- **无需 systemd / sudo / root**，Linux / macOS / Windows 通用
- **Docker 环境**同样适用，Bot 由 WebUI 内建管理器自动控制

### 房间自动恢复

房间的**期望运行状态**记在 `data/desired_states.json`：点「启动」就记 true，点「停止」就记 false，
与进程是否崩掉无关。软件重启、电脑关机再开机后，WebUI 起来会按这份状态自动拉起房间，
不需要手动一个个点启动。旧版留下的 `running_rooms.json` 会自动迁移过来。

重启后没起来先看两处：`rooms/<房间号>/data/bot_status.json`（有没有心跳、`credential_status` 是什么）、
`rooms/<房间号>/data/auth_status.json`（是不是要重新扫码）。

---

## 🪟 Windows .exe 构建与发布

### 从源码构建

```bash
pip install pyinstaller pystray pillow
python scripts/build_exe.py
```

构建产物输出在 `dist/Ayabot.exe`，约 60 MB。

### 代码签名（消除「无法验证发布者」）

未签名的 exe 从网上下载后，Windows 会弹「无法验证发布者，你确定要运行此软件吗？」。
两种处理：

1. **用户侧**：点「运行」即可；程序启动后会自动清掉下载标记，之后不再提示。
2. **彻底解决**：给 exe 做签名。本项目两条路，都会在 CI 里自动完成：

| 方式 | 成本 | 说明 |
|---|---|---|
| [SignPath Foundation](https://signpath.org/) | 免费 | 面向开源项目，证书签发给 SignPath Foundation（发布者显示为该基金会）。每个版本需在 SignPath 后台人工批准一次签名请求。申请入径见 [SIGNING.md](SIGNING.md) |
| 自购证书（pfx） | €25~400/年 | 配 `WIN_CERT_BASE64` + `WIN_CERT_PASSWORD` 两个 secret，CI 用 signtool 签名（带时间戳） |

签名配置与否都不影响构建：没配就是未签名产物，配了自动签并校验。
GitHub Actions 里的三个 secret：

| Secret | 用途 |
|---|---|
| `SIGNPATH_API_TOKEN` | SignPath API 令牌（批准后在其后台生成） |
| `SIGNPATH_ORG_ID` | SignPath 组织 ID |
| `WIN_CERT_BASE64` / `WIN_CERT_PASSWORD` | 自购 pfx 证书（可选，与 SignPath 二选一） |

签名完成后的效果：文件属性里有「数字签名」页，发布者显示为 SignPath Foundation；
SmartScreen 不再把它当未知程序拦。

**Code signing policy**：[SIGNING.md](SIGNING.md)（签名对象、团队角色、构建流程、隐私政策）

### 嵌入的资源

exe 包含以下内容，启动时自动释放到 `%LOCALAPPDATA%\Ayabot\`：
- `config.example.yaml`（作为默认配置模板，首次启动自动复制）
- `figs/icon.png`（托盘图标）

运行所需的数据目录结构：

```
%LOCALAPPDATA%\Ayabot\
├── config.yaml        ← 自动从 exe 释放（可自行修改）
├── ayabot.log         ← 自动轮转，最多保留 5000 行
├── port.txt           ← 端口持久化
├── running_rooms.json ← 运行中的房间列表
└── data\
    ├── users.json     ← 用户账号
    ├── templates.json ← 预设模板
    └── bot.db         ← 全局数据库
```

### 托盘功能

| 菜单 | 说明 |
|------|------|
| 🌐 **打开管理后台** | 浏览器打开 WebUI |
| 🔌 **设置端口** | 修改监听端口（默认 19810） |
| 🔑 **重置管理员密码** | 重置为 123456 |
| 📋 **查看日志** | 级别筛选、自动刷新、自动滚动、深色主题 |
| ❌ **退出** | 保存运行中的房间后退出 |

---

## 💖 致谢

- [bilibili-api-python](https://github.com/Nemo2011/bilibili-api) — B 站 API 的 Python 封装，本项目的弹幕监听、礼物解析等功能基于此库实现

---

## 📄 License

GNU General Public License v3.0 — 详见 [LICENSE](LICENSE)。

本软件是自由软件，您可以自由使用、修改和分发，但**任何衍生作品也必须以 GPLv3 开源**。

---

## ☕ 打赏支持

如果觉得 Ayabot 有帮助，欢迎请作者喝杯咖啡 ❤️

<p align="center">
  <img src="https://raw.githubusercontent.com/yujianke100/Ayabot/main/figs/alipay.jpg" width="200" alt="支付宝打赏">
  <img src="https://raw.githubusercontent.com/yujianke100/Ayabot/main/figs/wechat.png" width="200" alt="微信打赏">
</p>
