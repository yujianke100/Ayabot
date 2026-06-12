# bilibili-live-robot

轻量 B 站直播间弹幕机器人（单直播间、低资源常驻）。支持自动欢迎、送礼感谢、大航海答谢、盲盒统计、签到抽签、AI 智能回复（LLM 驱动）、自定义关键词回复，以及带登录认证的 Web 管理后台。

---

## 快速开始

### 方式一：Python 本地运行

```bash
# 1. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置
cp config.example.yaml config.yaml
# 编辑 config.yaml，填写房间号、主播 UID、B 站 Cookie 等

# 4. 启动
python -m app.main
```

首次启动会打印二维码，使用 Bilibili App 扫码登录，凭据自动保存到 `data/credential.json`，后续重启自动复用。

> 也可在 `config.yaml` 中直接填入 `credential` 下的 Cookie 字段（SESSDATA / bili_jct / buvid3 / DedeUserID）跳过扫码。

### 方式二：Docker（推荐）

```bash
docker run -d \
  --name bili-bot \
  -v /path/to/config.yaml:/app/config.yaml \
  -v /path/to/data:/app/data \
  -p 8000:8000 \
  ghcr.io/yujianke100/bilibili-live-robot:latest
```

或使用 docker-compose：

```yaml
version: "3"
services:
  bili-bot:
    image: ghcr.io/yujianke100/bilibili-live-robot:latest
    container_name: bili-bot
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./config.yaml:/app/config.yaml
      - ./data:/app/data
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
| **📊 送礼排行** | 按日期范围查看送礼排行（支持礼物/盲盒/全部），含柱状图 |
| **🎨 精美导出** | 按用户导出仿 B 站送礼明细卡片，自定义列宽和每列行数 |
| **⚙️ 机器人配置** | 直播间 ID、主播 UID、冷却时间、限流参数、功能开关、回复模板、定时消息、机器人名称（需重启生效） |
| **🤖 AI 回复设置** | 开关、API 密钥、模型、唤醒词、人设、上下文参数（保存即时生效） |
| **🗑️ 数据管理** | 删除指定日期之前的历史数据 |

---

## 配置详解

### `config.yaml` 主要字段

```yaml
# 直播间 & 主播
room_display_id: 1946287911      # 直播间号
anchor_uid: 1000000              # 主播 UID

# B 站登录凭据（二选一：Cookie 或扫码）
credential:
  sessdata: ""                   # 浏览器 Cookie
  bili_jct: ""
  buvid3: ""
  dedeuserid: ""

# 功能开关
features:
  welcome_enabled: true          # 欢迎
  thanks_enabled: true           # 送礼感谢
  blindbox_enabled: true         # 盲盒统计
  guard_thanks_enabled: true     # 大航海感谢
  connected_message_enabled: true
  periodic_message_enabled: true # 定时消息
  periodic_message_interval_seconds: 600  # 间隔（秒）
  periodic_message_template: "欢迎关注直播间~点个关注不迷路！"

# Web 管理后台
web_ui:
  enabled: true
  host: "0.0.0.0"
  port: 8000
  username: "admin"
  password: "your_password"
  bot_name: "bilibot"            # 机器人名称（影响 UI 标题和帮助文本）

# LLM / AI 回复
llm:
  enabled: false
  provider: "openai"             # "openai" 或 "anthropic"
  api_key: ""
  base_url: "https://api.openai.com/v1"
  model: "gpt-4o-mini"
  wake_word: "bilibot"           # 弹幕唤醒词
  system_prompt: "你是bilibot，一个可爱温柔的虚拟主播助手。"
  temperature: 0.7
  top_p: 0.9
  max_tokens: 150
  context:
    enabled: true
    mode: "isolated"             # "isolated"=按用户 / "merged"=合并
    content: "llm_only"          # "llm_only"=仅AI对话 / "all"=所有弹幕
    max_messages: 10
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

## Docker 构建

代码推送到 `main` 分支后，GitHub Actions 自动构建并推送到 [ghcr.io](https://github.com/yujianke100/bilibili-live-robot/pkgs/container/bilibili-live-robot)：

```bash
# 拉取最新镜像
docker pull ghcr.io/yujianke100/bilibili-live-robot:latest
```

也可手动构建：

```bash
docker build -t bilibili-live-robot .
```

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
