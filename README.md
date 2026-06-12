# bilibot

轻量 B 站直播间弹幕机器人（单直播间、低资源常驻）。支持自动欢迎、送礼感谢、大航海答谢、盲盒统计、签到抽签、AI 智能回复（LLM 驱动）、自定义关键词回复，以及带登录认证的 Web 管理后台。

---

## 快速开始

### 方式一：Docker（推荐）

```bash
mkdir bilibot && cd bilibot
wget https://raw.githubusercontent.com/yujianke100/bilibot/main/config.example.yaml -O config.yaml
# 编辑 config.yaml 填写房间号、主播 UID、B 站 Cookie

docker run -d \
  --name bili-bot \
  -v ./config.yaml:/app/config.yaml \
  -v ./data:/app/data \
  -p 8000:8000 \
  yujianke100/bilibot:latest
```

首次启动会打印二维码，使用 Bilibili App 扫码登录，凭据自动保存到 `data/credential.json`。

或使用 docker-compose：

```yaml
# docker-compose.yml
services:
  bili-bot:
    image: yujianke100/bilibot:latest
    container_name: bili-bot
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./config.yaml:/app/config.yaml
      - ./data:/app/data
    environment:
      - TZ=Asia/Shanghai
```

```bash
docker compose up -d
```

### 方式二：Python 本地运行

```bash
git clone https://github.com/yujianke100/bilibot.git
cd bilibot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# 编辑 config.yaml

python -m app.main
```

---

## 🔐 B 站登录

首次启动或 Cookie 过期时，机器人会在终端打印二维码，扫码后自动保存凭据。

**Web UI 也可扫码登录**：登录 Web 管理后台后，点击「B站登录」选项卡，显示二维码后用 Bilibili App 扫码，成功自动保存并重启服务。

也可在 `config.yaml` 中直接填写 Cookie 字段跳过扫码：

```yaml
credential:
  sessdata: "你的SESSDATA"
  bili_jct: "你的bili_jct"
  buvid3: "你的buvid3"
  dedeuserid: "你的DedeUserID"
```

---

## 核心功能

### 弹幕命令

| 指令 | 说明 |
|------|------|
| `#签到` | 每日签到（按直播场次计算连续天数） |
| `#抽签` | 今日运势抽签 |
| `#今日盲盒` | 今日盲盒统计 |
| `#本月盲盒` / `#盲盒统计` | 本月盲盒汇总 |
| `#本月盲盒:用户昵称` | 查询指定用户的盲盒汇总 |
| `#bilibot <聊天内容>` | AI 智能回复（唤醒词和聊天内容可自定义） |
| `#帮助` | 显示所有命令 |

> 所有中文指令支持全角/半角 `#`，如 `＃签到` 也可识别。

### 定时消息

开播期间自动发送关注提醒消息，间隔可在 Web 管理后台配置（默认 10 分钟）。

### 自动事件

| 事件 | 行为 |
|------|------|
| **用户进入** | 发送欢迎弹幕（可配置模板、冷却时间） |
| **送礼** | 感谢送礼；盲盒额外播报盈亏 |
| **上舰/提督/总督** | 播报大航海开通（不同等级独立模板） |
| **连接成功** | 发送上线消息（可配置模板、开关） |

### 关键词回复

支持多关键词正则匹配，可配置冷却时间、多组规则。

### AI 智能回复（LLM）

- 自定义唤醒词（如 `#文文`、`#bilibot`）
- 支持 OpenAI / Anthropic 格式接口
- 可自定义人设（System Prompt）
- 内置三层防注入保护（预过滤 + 消息包裹 + 不可绕过的安全规则）
- 可配置的上下文记忆（按用户隔离/合并、最大条数）
- 支持 Web UI 在线测试

### 机器人名称

所有 Web UI 标题、弹幕帮助提示、AI 回复人设均使用可配置的机器人名称（`config.yaml` → `web_ui.bot_name`，也可在 Web 管理后台设置）。

### Web 管理后台

随机器人一同启动，浏览器访问 `http://localhost:8000`：

| 模块 | 功能 |
|------|------|
| **🔐 登录** | 账号密码认证（可在 `config.yaml` 修改），Session 有效期可配置 |
| **🅱️ B站登录** | 扫码登录 B 站（凭据过期或首次启动时使用） |
| **📊 送礼排行** | 按日期范围查看送礼排行（支持礼物/盲盒/全部），含柱状图 |
| **🎨 精美导出** | 按用户导出仿 B 站送礼明细卡片，自定义列宽和每列行数 |
| **⚙️ 机器人配置** | 直播间 ID、主播 UID、冷却时间、限流参数、功能开关、回复模板、定时消息、机器人名称（需重启生效） |
| **🤖 AI 回复设置** | 开关、API 密钥、模型、唤醒词、人设、上下文参数（保存即时生效） |
| **🗑️ 数据管理** | 删除指定日期之前的历史数据 |

---

## 配置详解

### `config.yaml` 主要字段

```yaml
room_display_id: 1946287911      # 直播间号
anchor_uid: 1000000              # 主播 UID

credential:
  sessdata: ""                   # B 站 Cookie（二选一：填写或首次扫码）
  bili_jct: ""
  buvid3: ""
  dedeuserid: ""

features:
  welcome_enabled: true
  thanks_enabled: true
  blindbox_enabled: true
  guard_thanks_enabled: true
  connected_message_enabled: true
  periodic_message_enabled: true
  periodic_message_interval_seconds: 600
  periodic_message_template: "欢迎关注直播间~点个关注不迷路！"

web_ui:
  enabled: true
  host: "0.0.0.0"
  port: 8000
  username: "admin"
  password: "your_password"
  bot_name: "bilibot"            # 机器人名称（UI 标题 + 帮助文本）

llm:
  enabled: false
  provider: "openai"             # "openai" 或 "anthropic"
  api_key: ""
  base_url: "https://api.openai.com/v1"
  model: "gpt-4o-mini"
  wake_word: "bilibot"
  system_prompt: "你是bilibot，一个可爱温柔的虚拟主播助手。"
  temperature: 0.7
  top_p: 0.9
  max_tokens: 150
  context:
    enabled: true
    mode: "isolated"
    content: "llm_only"
    max_messages: 10
```

---

## Docker 构建

代码推送到 `main` 分支后，GitHub Actions 自动构建并推送到：

- **Docker Hub**（主）: `yujianke100/bilibot:latest`
- **ghcr.io**（备用）: `ghcr.io/yujianke100/bilibot:latest`

```bash
docker pull yujianke100/bilibot:latest
```

### 🇨🇳 国内拉取加速

Docker Hub 在国内有多个公共镜像站，可替代 `docker.io` 前缀直接拉取：

```bash
# 实用镜像站
docker pull hub-mirror.c.163.com/yujianke100/bilibot:latest
docker pull docker.mirrors.ustc.edu.cn/yujianke100/bilibot:latest
```

> ⚠️ 镜像站偶有失效。查看最新可用镜像站：
> **[https://demo.kentxxq.com/app/mirror](https://demo.kentxxq.com/app/mirror)**

**配置 Docker Daemon 全局镜像加速**（推荐，一劳永逸）：

编辑 `/etc/docker/daemon.json`：

```json
{
  "registry-mirrors": [
    "https://docker.mirrors.ustc.edu.cn",
    "https://hub-mirror.c.163.com"
  ]
}
```

然后 `systemctl restart docker`，之后 `docker pull` 命令自动走镜像。

**手动构建**：

```bash
git clone https://github.com/yujianke100/bilibot.git
cd bilibot
docker build -t bili-bot .
```

---

## 礼物卡片导出

在 Web 后台的「精美导出」页面：

1. 输入 UID 或从排行点击用户，日历上蓝色高亮的日期为有数据的日期
2. 选择日期，点击「生成」
3. 卡片自动按送礼时间排序，相邻同礼物 2 分钟内自动合并
4. 毛玻璃质感卡片：普通用户深灰色、舰长蓝色、提督紫色、总督金色
5. 舰长头像框（B 站真实图片）
6. 使用 `Ctrl+P` / `Cmd+P` 打印为 PDF，或使用浏览器截图

---

## 数据存储

- **数据库**: SQLite（默认 `data/bot.db`），所有事件自动入库，**永不自动删除**
- **登录凭据**: `data/credential.json`
- **配置**: `config.yaml`

---

## 技术栈

- **运行**: Python 3.11+, asyncio
- **B 站 API**: [bilibili-api-python](https://github.com/Nemo2011/bilibili-api)
- **Web UI**: FastAPI + Vue 3 + Tailwind CSS + Chart.js
- **LLM API**: OpenAI / Anthropic 兼容接口
- **存储**: SQLite

---

## License

MIT
