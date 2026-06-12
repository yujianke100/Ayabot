<p align="center">
  <img src="logo.png" alt="ayabot" width="200" />
</p>

# ayabot

<p align="center">
  <a href="https://github.com/yujianke100/ayabot/releases"><img src="https://img.shields.io/github/v/release/yujianke100/ayabot" alt="Release"></a>
  <a href="https://github.com/yujianke100/ayabot/pkgs/container/ayabot"><img src="https://img.shields.io/badge/docker-ghcr.io-blue?logo=github" alt="ghcr.io"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow" alt="License"></a>
</p>

B 站直播间弹幕机器人，支持签到抽签、盲盒统计、AI 对话、自动欢迎/感谢、定时消息、Web 后台管理。开箱即用，小白友好。

---

## 🚀 快速上手

### 把机器人跑起来（Docker，推荐）

```bash
# 1. 创建文件夹，下载配置模板
mkdir ayabot && cd ayabot
wget https://raw.githubusercontent.com/yujianke100/ayabot/main/config.example.yaml -O config.yaml

# 2. 编辑 config.yaml，填三个东西：
#    房间号（room_display_id）和主播 UID（anchor_uid），在直播间 URL 里能看到
#    Web 后台密码（web_ui.password），设一个你自己记得的

# 3. 启动！
docker run -d \
  --name ayabot \
  -v ./config.yaml:/app/config.yaml \
  -v ./data:/app/data \
  -p 8000:8000 \
  ghcr.io/yujianke100/ayabot:latest
```

启动后浏览器打开 `http://你的IP:8000` 就能看到 Web 后台了。

第一次启动时，机器人会在终端打印一个二维码，用 B 站 App 扫码登录。之后会自动保存凭据，下次重启不用再扫。

> **国内拉取镜像慢？** 见下方[镜像加速](#-docker-镜像)章节。

### 或用 Python 直接跑

```bash
git clone https://github.com/yujianke100/ayabot.git
cd ayabot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# 编辑 config.yaml ...
python -m app.main
```

---

## 🎯 直播间弹幕命令

在直播间发以下指令，机器人自动回复：

| 发这个 | 机器人会 |
|--------|----------|
| `#签到` | 签到 + 显示连续天数 + 排名 |
| `#抽签` | 今日运势 |
| `#今日盲盒` | 统计今日送的盲盒 |
| `#本月盲盒` | 本月盲盒汇总 |
| `#ayabot <说点啥>` | AI 聊天（需要在后台先配置 API Key） |
| `#帮助` | 列出所有命令 |

> 中文 `#` 和英文 `#` 都行，`＃签到` 也能识别。
> 机器人的名字（唤醒词）可以在 Web 后台 → AI 回复设置 里自由修改。

---

## ⚙️ Web 管理后台

浏览器打开 `http://你的IP:8000` 进入后台（账号密码在 config.yaml 里配置）。

| 页面 | 能干嘛 |
|------|--------|
| 📊 **送礼排行** | 按日期看谁送了多少礼物/盲盒，带柱状图 |
| 🎨 **精美导出** | 导出送礼明细卡片，可直接打印或截图 |
| 🤖 **AI 回复** | 开关、改唤醒词、配 API Key、调温度、改人设 |
| ⚙️ **机器人配置** | 改房间号、冷却时间、欢迎/感谢模板、定时消息 |
| 🅱️ **B站登录** | 扫码登录（凭据过期时用） |
| 🗑️ **数据管理** | 删旧数据 |

---

## 📝 config.yaml 快速说明

```yaml
room_display_id: 1946287911        # ← 改成你的直播间号
anchor_uid: 1000000                # ← 改成主播的 UID

web_ui:
  username: "admin"                # 后台登录账号
  password: "设一个复杂密码"        # ← 记得改！
  bot_name: "ayabot"               # 机器人名字

llm:
  enabled: false                   # AI 对话，在后台开启即可
```

完整的配置说明见 [config.example.yaml](config.example.yaml)。

---

## 🐳 Docker 镜像

自动构建推送到 GitHub Container Registry（ghcr.io）：

```bash
docker pull ghcr.io/yujianke100/ayabot:latest     # 最新版
docker pull ghcr.io/yujianke100/ayabot:<sha>       # 指定版本（在 Actions 构建日志里看）
```

也支持 Docker Hub（需在 GitHub Secrets 中配置 `DOCKER_USERNAME` + `DOCKER_PASSWORD` 后才会推送）：

```bash
# ⚠️ 需要管理员在 GitHub 仓库 Settings → Secrets 配好 Docker Hub 凭据才会推送
docker pull yujianke100/ayabot:latest
```

如果你在 GitHub 上，可以直接点 badge 去看镜像包：

[![ghcr.io](https://img.shields.io/badge/docker-ghcr.io-blue?logo=github)](https://github.com/yujianke100/ayabot/pkgs/container/ayabot)

### 🇨🇳 国内镜像加速

ghcr.io 国内拉取可能很慢，推荐以下方式：

**方式一：配置 Docker 全局镜像加速**（改镜像源为国内源）

编辑 `/etc/docker/daemon.json`：

```json
{
  "registry-mirrors": [
    "https://docker.mirrors.ustc.edu.cn",
    "https://hub-mirror.c.163.com"
  ]
}
```

> ⚠️ 注意：registry-mirrors 只对 Docker Hub（`docker.io`）生效，对 `ghcr.io` 无效。ghcr.io 国内加速见下方。

**方式二：通过 ghcr.io 镜像站拉取**

```bash
# 中科大 ghcr.io 镜像
docker pull docker.mirrors.ustc.edu.cn/ghcr.io/yujianke100/ayabot:latest

# 网易 ghcr.io 镜像
docker pull hub-mirror.c.163.com/ghcr.io/yujianke100/ayabot:latest
```

**方式三：查询最新可用镜像站**
→ [demo.kentxxq.com/app/mirror](https://demo.kentxxq.com/app/mirror)（实时检测国内各镜像站可用性）

---

## 💾 数据文件

| 文件 | 说明 |
|------|------|
| `data/bot.db` | SQLite 数据库，所有签到/盲盒/送礼数据都在这里 |
| `data/credential.json` | B 站登录凭据，扫码后自动生成 |
| `config.yaml` | 所有配置 |

数据都不会自动删除，可以在 Web 后台手动清理。

---

## 📄 License

MIT
