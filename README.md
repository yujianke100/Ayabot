<p align="center">
  <img src="logo.png" alt="ayabot" width="200" />
</p>

# ayabot

<p align="center">
  <a href="https://github.com/yujianke100/ayabot/releases"><img src="https://img.shields.io/github/v/release/yujianke100/ayabot" alt="Release"></a>
  <a href="https://github.com/yujianke100/ayabot/pkgs/container/ayabot"><img src="https://img.shields.io/badge/docker-ghcr.io-blue?logo=github" alt="ghcr.io"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow" alt="License"></a>
</p>

B 站直播间弹幕机器人，支持签到抽签、盲盒统计、AI 对话、自动欢迎/感谢、定时消息、Web 后台管理。开箱即用，小白友好。纯 Python 跨平台实现，无需 systemd / sudo。

---

## 初始账号

| 用户名 | 密码 | 说明 |
|--------|------|------|
| `ayabot` | `123456` | 管理员账号，首次登录强制修改密码 |

> 密码忘记或想重置？运行 `python -m app.reset_admin` 即可重置为随机密码。

---

## 🚀 快速上手

### 用 Python 直接跑（推荐开发）

```bash
git clone https://github.com/yujianke100/ayabot.git
cd ayabot
pip install -r requirements.txt
cp config.example.yaml config.yaml
# 编辑 config.yaml：填直播间号和主播 UID
python -m app.main
```

启动后浏览器打开 `http://localhost:19810` 进入 Web 后台。

> **端口被占用了？** 启动前运行 `export AYABOT_PORT=你想要的端口号` 即可修改。

第一次启动时，机器人会在终端打印二维码，用 B 站 App 扫码登录。之后会自动保存凭据。

### 或用 Docker 跑

```bash
mkdir ayabot && cd ayabot
wget https://raw.githubusercontent.com/yujianke100/ayabot/main/config.example.yaml -O config.yaml
# 编辑 config.yaml，填直播间号和主播 UID
docker run -d \
  --name ayabot \
  -v ./config.yaml:/app/config.yaml \
  -v ./data:/app/data \
  -p 19810:19810 \
  -e AYABOT_PORT=19810 \
  ghcr.io/yujianke100/ayabot:latest
```

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
| `#文文 <说点啥>` | AI 聊天（唤醒词可在后台修改） |
| `#帮助` | 列出所有命令 |

> 中文 `#` 和英文 `#` 都行，`＃签到` 也能识别。
> 开启「免#指令」后，不带 `#` 前缀也能触发（如直接发「签到」）。
> 开启「AI免#前缀唤醒」后，弹幕以唤醒词开头即触发 AI 回复。

---

## ⚙️ Web 管理后台

浏览器打开 `http://你的IP:19810` 进入后台（初始账号 `ayabot` / `123456`，首次登录强制修改）。

| 页面 | 能干嘛 |
|------|--------|
| 📊 **送礼排行** | 按日期看谁送了多少礼物/盲盒，带柱状图 |
| 🎨 **精美导出** | 导出送礼明细卡片，可直接打印或截图 |
| 🤖 **AI 回复** | 开关、改唤醒词、配 API Key、调温度、改人设。支持三种触发方式 |
| ⚙️ **机器人配置** | 功能开关、冷却时间、弹幕限流、欢迎/感谢/盲盒模板、定时消息、关键词回复、签文 |
| 👥 **用户管理** | 管理员增删改查、B站账号登录/验证 |
| 💬 **弹幕记录** | 查看直播间历史弹幕 |
| 🗑️ **数据管理** | 清理旧数据 |

### 功能开关

| 功能 | 说明 |
|------|------|
| 免#指令 | 不带 `#` 也能触发签到、盲盒等指令 |
| AI免#前缀唤醒 | 弹幕以唤醒词开头即触发 AI 回复 |
| 包含关键词触AI | 弹幕任何位置含唤醒词即触发 AI 回复 |
| 弹幕记录 | 开启后记录所有弹幕到数据库，可在 Web 查看 |
| 大航海欢迎/感谢 | 舰长/提督/总督专属模板 |
| 自定义签文 | 修改抽签结果的六种签文内容 |
| 盲盒统计（今日/本月） | 区分今日和本月的盲盒统计，支持自定义回复文本 |

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

| 文件 | 说明 |
|------|------|
| `rooms/<id>/data/bot.db` | 房间独立 SQLite 数据库 |
| `rooms/<id>/bot.pid` | Bot 进程 PID 文件 |
| `rooms/<id>/bot.log` | Bot 运行日志 |
| `accounts/<uid>/credential.json` | B 站扫码登录凭据 |
| `data/users.json` | WebUI 用户账号 |
| `data/templates.json` | 预设模板 |

### 跨平台进程管理

纯 Python 实现（`app/process_manager.py`），用 `subprocess.Popen` 管理 Bot 子进程：
- **无需 systemd**，Linux / macOS / Windows 通用
- **无需 sudo / root**
- Docker 环境同样适用

---

## 🐳 Docker

自动构建推送到 GitHub Container Registry (ghcr.io)：

```bash
docker pull ghcr.io/yujianke100/ayabot:latest
```

---

## 📄 License

MIT
