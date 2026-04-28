# B站直播间轻量机器人开发文档

## 1. 目标与约束

### 1.1 目标

实现一个可在 1G2C Ubuntu 服务器常驻的轻量机器人，围绕单个指定主播直播间提供以下能力：

1. 主播开播后自动进入直播间。
2. 主动 @ 并欢迎进入直播间的观众。
3. 观众送礼后 @ 该观众感谢。
4. 若礼物为盲盒，主动提示收益：

$$收益 = 实际礼物价值 - 盲盒成本$$

5. 统计本月每个送过盲盒的观众：
   - 总送盲盒数量
   - 总收益
   - 总成本
   - 总实际礼物价值

### 1.2 非目标

1. 不做多直播间集群。
2. 不做可视化后台（初版）。
3. 不做复杂风控对抗。

### 1.3 合规提示

1. 仅用于合规、文明互动，不用于刷屏、骚扰、恶意营销。
2. 注意账号风控：控制发送频率，避免高频重复弹幕。
3. Cookie/凭据必须安全存储，不写入公开仓库。

## 2. 可行性结论

结论：可行，且可以做成非常轻量。

基于 bilibili-api（Nemo2011）当前文档与代码能力：

1. 可通过 `LiveDanmaku` 长连接实时接收直播间事件（弹幕、礼物、进场、开播/下播等）。
2. 可通过 `LiveRoom.send_danmaku(..., reply_mid=uid)` 发送带 @ 的弹幕回复。
3. 可订阅 `SEND_GIFT` / `COMBO_SEND` 事件处理送礼感谢与统计。
4. 可通过 `INTERACT_WORD_V2`（以及 `WELCOME` / `WELCOME_GUARD`）监听用户进场。
5. 支持异步与自动重连，适合低资源常驻进程。

已确认的主要接口与事件（来自 `live.py` 文档与示例）：

1. `live.LiveDanmaku(room_display_id, credential=...)`
2. `LiveDanmaku.connect()` / `disconnect()`
3. `LiveRoom.send_danmaku(danmaku, reply_mid=uid)`
4. 事件：`LIVE`、`PREPARING`、`INTERACT_WORD_V2`、`SEND_GIFT`、`COMBO_SEND`、`DANMU_MSG`

## 3. 技术方案（轻量常驻）

### 3.1 架构

单进程异步架构：

1. `watcher`：检查主播是否开播（轮询）。
2. `ws_client`：开播时连接 `LiveDanmaku`，接收实时事件。
3. `message_handler`：欢迎、感谢、盲盒收益计算。
4. `command_handler`：主播弹幕指令（开关欢迎、修改欢迎词）。
5. `stats_store`：SQLite 持久化统计。

推荐技术栈：

1. Python 3.11+
2. bilibili-api-python
3. aiohttp（或默认支持的异步客户端）
4. SQLite（标准库）
5. systemd（进程守护）

### 3.2 资源预估（1G2C）

正常负载（单房间）预估：

1. CPU：常态低于 5%，弹幕高峰短时升高。
2. 内存：约 60MB 到 150MB（取决于日志级别与缓存策略）。
3. 磁盘：SQLite + 日志，月级别一般小于数百 MB。

结论：1G2C 可稳定承载。

## 4. 功能设计

### 4.1 开播后自动进入直播间

实现策略：

1. 轮询 `LiveRoom.get_room_play_info()`，检查开播状态字段。
2. 从未开播 -> 开播：创建 `LiveDanmaku` 并 `connect()`。
3. 从开播 -> 下播（`PREPARING` 事件或状态变更）：断开连接，回到轮询。
4. 轮询间隔建议 20 到 30 秒。

### 4.2 主动欢迎进场观众

触发事件优先级：

1. 主：`INTERACT_WORD_V2`
2. 备：`WELCOME` / `WELCOME_GUARD`

处理逻辑：

1. 提取 uid 与用户名。
2. 去重与限流：同一 uid 在 N 秒内仅欢迎一次（建议 600 秒）。
3. 发送欢迎弹幕：`send_danmaku(..., reply_mid=uid)`。

主播指令（建议）：

1. `#欢迎 开`：开启欢迎。
2. `#欢迎 关`：关闭欢迎。
3. `#欢迎 词 欢迎词`：更新欢迎词模板。

欢迎词模板示例：

1. `欢迎 {uname} 进入直播间！`
2. `嗨 {uname}，欢迎来玩～`

权限控制：

1. 仅主播本人 uid（可选加房管）可执行指令。

### 4.3 送礼感谢与盲盒收益提示

触发事件：

1. `SEND_GIFT`
2. `COMBO_SEND`（连击场景可合并或节流）

通用感谢：

1. 提取 uid、uname、giftName、num。
2. 发送 `@uid` 感谢语句。
3. 感谢文案支持模板配置：`features.thanks_template`。

盲盒收益：

1. 优先从礼物事件内识别盲盒相关字段（不同时期字段可能变化）。
2. 若可获得盲盒成本与实际礼物价值，计算：

$$profit = actual_value - blind_box_cost$$

3. 若是多连发可按总量计算：

$$profit_{total} = actual_{total} - cost_{total}$$

4. 发送提示示例：
   - `@{uname} 盲盒开出 {gift_name}，本次收益 {profit} 电池！`

注意：

1. 盲盒字段在不同事件版本中可能不一致，需在开发初期开启原始事件日志采样验证。
2. 采样后固定字段映射，再关闭详细日志。

### 4.4 本月盲盒统计

统计维度（按 uid + 月）：

1. blind_box_count：盲盒数量
2. cost_total：盲盒总成本
3. actual_total：实际礼物总价值
4. profit_total：总收益

月度定义：

1. 以服务器时区（建议 `Asia/Shanghai`）下自然月统计。

查询输出（可扩展弹幕指令，注意直播间弹幕不支持空格，统一无空格格式）：

1. `#盲盒统计`：用户个人当月汇总。
2. `#盲盒统计:用户名` 或 `#盲盒统计：用户名`：查询指定用户的当月盲盒汇总。

## 5. 数据库设计（SQLite）

建议最小表结构：

### 5.1 event_gifts

1. id INTEGER PK
2. ts INTEGER（秒级时间戳）
3. month TEXT（如 `2026-04`）
4. uid INTEGER
5. uname TEXT
6. event_type TEXT（SEND_GIFT/COMBO_SEND）
7. gift_name TEXT
8. gift_num INTEGER
9. is_blind_box INTEGER（0/1）
10. blind_box_cost INTEGER（单位按事件原值，建议统一到电池或金瓜子）
11. actual_value INTEGER
12. profit_value INTEGER
13. raw_json TEXT（可选，排障用）

索引建议：

1. `(month, uid)`
2. `(month, is_blind_box)`
3. `ts`

### 5.2 monthly_blindbox_stats（可选）

若不做物化表，也可查询时聚合 `event_gifts`。

## 6. 核心风控与稳定性

### 6.1 发言频率控制

1. 全局发言最小间隔（如 1.2 秒）。
2. 单用户触发冷却（欢迎、感谢分别冷却）。
3. 同内容去重（防止重复发送）。

### 6.2 连接稳定性

1. 断线自动重连（指数退避上限）。
2. 心跳超时后重连。
3. 异常分级日志，避免日志风暴。

### 6.3 故障自恢复

1. 任意事件处理失败不应导致主循环退出。
2. 数据库写入失败可重试并降级到文件缓冲。

## 7. 配置文件建议

建议 `config.yaml` 字段：

1. `room_display_id`
2. `anchor_uid`
3. `credential.sessdata`
4. `credential.bili_jct`
5. `credential.buvid3`
6. `credential.dedeuserid`（可选但建议）
7. `features.welcome_enabled`
8. `features.welcome_template`
9. `features.thanks_template`
10. `cooldown.welcome_user_seconds`
11. `cooldown.thanks_user_seconds`
12. `rate_limit.send_interval_seconds`
13. `timezone`
14. `db.path`
15. `log.level`

## 8. 部署方案（Ubuntu 常驻）

### 8.1 运行方式

1. 使用 venv 隔离环境。
2. 使用 systemd 守护，失败自动拉起。

### 8.2 systemd 服务示例

`/etc/systemd/system/bili-live-bot.service`：

```ini
[Unit]
Description=Bilibili Live Robot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/bilibili-live-robot
ExecStart=/opt/bilibili-live-robot/.venv/bin/python -m app.main
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### 8.3 日志

1. 统一走 stdout/stderr，交给 journald。
2. 错误级日志包含 event id/uid，便于追踪。

## 9. 实施顺序（建议）

1. 第一步：完成开播检测 + 连接直播间 + 打印关键事件。
2. 第二步：完成欢迎逻辑 + 主播指令开关。
3. 第三步：完成送礼感谢。
4. 第四步：完成盲盒识别与收益计算（先采样再固化字段）。
5. 第五步：完成月度统计与查询输出。
6. 第六步：部署 systemd 与稳定性压测。

## 10. 风险与待确认项

### 10.1 盲盒字段漂移风险

不同时间段/事件版本中盲盒相关字段可能变更。

应对：

1. 初期保留原始事件采样日志。
2. 通过真实样本固化字段映射。
3. 映射失败时降级：仅感谢，不报收益。

### 10.2 账号风控

高频欢迎+感谢容易触发限制。

应对：

1. 加强节流。
2. 控制话术长度与重复度。
3. 必要时分级开关（仅欢迎或仅感谢）。

## 11. 里程碑验收标准

### M1: 基础连通

1. 能自动识别开播并连上。
2. 能稳定接收 `INTERACT_WORD_V2`、`SEND_GIFT`。

### M2: 核心功能

1. 欢迎开关与欢迎词配置可用。
2. 送礼感谢可用。
3. 盲盒收益在可识别场景下可正确计算。

### M3: 统计与运维

1. 月度统计正确。
2. 服务重启后状态恢复。
3. systemd 常驻稳定 7 天以上。

## 12. 最小可运行版本定义（MVP）

MVP 包含：

1. 开播自动连接
2. 欢迎（含开关 + 模板）
3. 送礼感谢
4. 盲盒收益提示（字段识别失败时自动降级）
5. SQLite 月度统计

不包含：

1. Web 面板
2. 多房间
3. 分布式

## 13. B站账号与登录策略

### 13.1 是否需要新注册 B站账号

技术上：不强制。

工程上：强烈建议使用独立机器人账号。

原因：

1. 风险隔离（风控、封禁、凭据泄露不影响主号）。
2. 运维清晰（机器人行为与日常使用分离）。
3. 权限边界明确（只授予机器人必要权限）。

### 13.2 登录实现建议

推荐方案：Cookie 凭据注入。

1. 在浏览器登录机器人账号。
2. 提取 `SESSDATA`、`bili_jct`、`buvid3`、`DedeUserID`。
3. 注入配置（`config.yaml` 或环境变量）。
4. 程序启动后构造 `Credential`。

不建议：在服务器端做账号密码自动登录流程。

### 13.3 凭据安全要求

1. `config.yaml` 不入库。
2. 日志中禁止输出完整 Cookie。
3. 凭据失效时支持告警与热更新。

## 14. 房管提权 Flag

配置项：`features.allow_admin_as_anchor`

语义：

1. `false`：仅 `anchor_uid` 可执行控制指令。
2. `true`：房管可执行同等控制指令（欢迎开关、欢迎词模板修改等）。

实现建议：

1. 优先使用 `ROOM_ADMINS` 事件同步房管 UID 列表。
2. 辅助使用弹幕事件中的管理身份字段作为兜底判定。

## 15. 命令行扫码登录机制

### 15.1 目标

在不引入 GUI/WebUI 的前提下，提供可长期运行的登录机制。

### 15.2 流程

1. 启动时优先读取本地凭据文件。
2. 若本地凭据有效，直接进入业务逻辑。
3. 若无凭据或凭据失效，走 `login_v2.QrCodeLogin`。
4. 在命令行输出 `get_qrcode_terminal()` 二维码字符串。
5. 扫码确认后获取 `Credential`（含 `ac_time_value`）。
6. 凭据落盘，后续重启自动复用。

### 15.3 自动续期

1. 定时调用 `credential.check_valid()`。
2. 若包含 `ac_time_value`，调用 `credential.check_refresh()`。
3. 需要刷新时调用 `credential.refresh()`。
4. 刷新成功后覆盖落盘凭据。

### 15.4 失败处理

1. 二维码超时：提示重启并重新扫码。
2. 刷新失败：记录告警并继续监听，必要时人工重登。
3. 凭据失效：重新扫码登录。
