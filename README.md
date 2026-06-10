# bilibili-live-robot

轻量 B 站直播间机器人（单直播间、低资源常驻）。

支持自动欢迎、送礼感谢、大航海答谢、盲盒统计、PK 战报、签到抽签、自定义关键词回复，以及带登录认证的 Web 管理后台。

---

## 快速开始

### 1. 准备 Python 环境（推荐）

```bash
# 创建专用虚拟环境
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# 或 .venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt
```

> 使用虚拟环境可避免依赖冲突，也方便 systemd 直接引用 `.venv/bin/python`。
> 每次操作前记得 `source .venv/bin/activate`，或直接使用 `.venv/bin/python` 运行。

### 2. 配置

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml，填写房间号、主播 UID 等
```

### 3. 启动

```bash
python -m app.main
```

首次启动会打印二维码，使用 Bilibili App 扫码登录，凭据自动保存到 `data/credential.json`，后续重启自动复用。

> 也可在 `config.yaml` 中直接填入 `credential` 下的 Cookie 字段（SESSDATA / bili_jct / buvid3 / DedeUserID）跳过扫码。

---

## 核心功能

### 弹幕互动

| 指令 | 说明 |
|------|------|
| `#签到` | 签到，返回连续签到天数和排名 |
| `#抽签` | 随机抽签，返回运势（大吉/吉/中吉/小吉/末吉/凶/大凶） |
| `#盲盒统计` | 查询自己本月的盲盒汇总（抽了多少、盈亏多少） |
| `#盲盒统计:用户昵称` | 查询指定用户的盲盒汇总 |
| `#欢迎 开` / `#欢迎 关` | 开关用户进入欢迎 |
| `#欢迎 词 <模板>` | 自定义欢迎词，可用 `{uname}` 占位 |
| `#关键词回复` | 自动回复（需在 `config.yaml` 中配置规则） |

> 所有中文指令支持全角/半角 `#`，支持可选空格，如 `#签到` / `＃签到` / `# 签到` 均可识别。

### 自动事件

| 事件 | 行为 |
|------|------|
| **用户进入** | 发送欢迎弹幕（可配置模板、冷却时间） |
| **送礼** | 感谢送礼；盲盒礼物额外播报盈亏 |
| **上舰/提督/总督** | 播报大航海开通（区分等级，独立模板） |
| **PK 开始** | 播报对面主播昵称、舰长数、在线人数 |
| **直播间开播/下播** | 自动连接/断开弹幕 WebSocket |

### Web 管理后台

随机器人一同启动，浏览器访问 `http://localhost:8000`：

| 模块 | 功能 |
|------|------|
| **🔐 登录** | 账号 `admin` / 密码 `admin`（可在 `config.yaml` 修改）；Session 一小时有效；60req/min 速率限制 |
| **📊 送礼排行** | 选日期范围、筛选（全部/一般礼物/盲盒），查看 Top 20 送礼排行 + 柱状图 |
| **🎨 精美导出** | 输入 UID 或从排行点击用户，日历选日期（有数据的蓝色高亮），生成仿 B 站送礼明细卡片；自定义单列宽度、每列行数 |
| **🗑️ 数据管理** | 删除某日期之前的历史数据（不可恢复） |

#### 礼物卡片特性

- 自动按送礼时间排序，相邻的相同礼物 2 分钟内自动合并
- 毛玻璃质感卡片：普通用户深灰色、舰长蓝色、提督紫色、总督金色
- 舰长头像框（B 站真实图片）
- 礼物图标自动从 B 站官方 API 获取
- COMBO_SEND 连击事件自动过滤，避免重复计数

### 礼物统计

所有礼物事件（包括普通礼物和盲盒）自动入库，按月汇总，**永不自动删除**，支持手动清理。

---

## 配置详解

### `config.yaml`

```yaml
room_display_id: 房间号
anchor_uid: 主播 UID

features:
  welcome_enabled: true                    # 开启进入欢迎
  welcome_template: "欢迎 {uname} ~"       # 欢迎模板
  thanks_enabled: true                     # 开启送礼感谢
  thanks_template: "感谢 {uname} 的 {gift_name} x{gift_num}"
  blindbox_enabled: true                   # 盲盒额外播报盈亏
  guard_thanks_enabled: true               # 开启大航海答谢
  allow_admin_as_anchor: true              # 房管可执行控制指令
  connected_message: "上线啦～"             # 连接成功提示
  connected_message_enabled: false         # 关闭连接提示
  keyword_reply:                           # 关键词自动回复
    enabled: false
    cooldown: 30
    rules:
      - keywords: ["群", "QQ群"]
        reply: "粉丝群：XXXXXX"
        match_mode: "contains"
```

---

## Web UI 访问

机器人启动后，Web UI 以 asyncio 任务形式运行在同一进程中。

```bash
# 本地访问
http://localhost:8000

# 远程访问（需防火墙放行对应端口）
http://<服务器IP>:<port>
```

默认端口 8000，默认凭据 `admin` / `admin`，可在 `config.yaml` 的 `web_ui` 段中修改：

```yaml
web_ui:
  enabled: true
  host: "0.0.0.0"
  port: 8000
  username: "admin"          # WebUI 登录账号
  password: "admin"          # WebUI 登录密码
  session_timeout: 3600       # Session 过期时间（秒）
  title: "BILIBILI-LIVE-ROBOT"  # 浏览器标签标题 & UI 左上角标题
```

---

## 部署

### systemd（Linux）

```ini
[Unit]
Description=Bilibili Live Robot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/bilibili-live-robot
ExecStart=/root/bilibili-live-robot/.venv/bin/python -m app.main
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable bili-live-bot
sudo systemctl start bili-live-bot
sudo journalctl -u bili-live-bot -f
```

---

## 项目结构

```
config.yaml              # 配置文件（勿提交）
config.example.yaml      # 配置模板
requirements.txt         # Python 依赖
app/
  main.py                # 入口（同时启动机器人和 WebUI）
  bot.py                 # 核心机器人逻辑
  storage.py             # SQLite 数据库层
  config.py              # 配置加载
  auth.py                # 认证管理
  web/
    server.py            # FastAPI WebUI 服务器
data/
  bot.db                 # SQLite 数据库（礼物统计/签到等）
  credential.json        # 扫码登录凭据
```

---

## 安全须知

1. **不要将 `config.yaml` 提交到仓库**，它包含 Cookie、Web UI 密码等敏感信息。
2. 建议在对外暴露时修改 `config.yaml` 中 `web_ui` 段的 `username` / `password`。
3. 有内置速率限制（60 req/min/IP），防止暴力破解。
4. 建议使用独立 B 站小号而非主号运行机器人。