# bilibili-live-robot

轻量 B 站直播间机器人（单直播间、低资源常驻）。

开发文档见：

- [docs/development.md](docs/development.md)

## Quick Start

1. 安装依赖：`pip install -r requirements.txt`
2. 复制配置：`copy config.example.yaml config.yaml`（Windows）或 `cp config.example.yaml config.yaml`（Linux）
3. 填写 `config.yaml` 里的房间号、主播 UID
4. 首次启动可不填 Cookie，程序会在命令行打印二维码用于扫码登录
4. 启动：`python -m app.main`

首次扫码成功后，凭据会写入 `auth.credential_store_path`（默认 `data/credential.json`），后续重启自动复用。

## 命令行扫码登录

1. 启动程序后，会打印终端二维码。
2. 使用 Bilibili App 扫码并确认登录。
3. 登录成功后自动进入正常运行。
4. 二维码超时时，重启程序重新扫码。

## 自动刷新

1. 程序会按 `auth.refresh_interval_seconds` 定时检查凭据状态。
2. 检测需要刷新时调用 `credential.refresh()` 并落盘新凭据。
3. 若无 `ac_time_value`，则只能复用现有 Cookie，失效后需重新扫码。

## 账号与登录建议

1. 建议使用独立 B 站机器人账号，不建议直接用主号。
2. 登录方式建议为：浏览器登录后提取 Cookie（SESSDATA / bili_jct / buvid3 / DedeUserID），写入 `config.yaml`。
3. 严禁把 `config.yaml` 提交到仓库。

## 房管提权 Flag

配置项：`features.allow_admin_as_anchor`

1. `false`：仅主播 UID 可执行控制指令。
2. `true`：房管可获得与主播相同的控制指令权限（如欢迎开关、欢迎词修改）。

## 指令（中文）

1. `#欢迎 开`：开启欢迎。
2. `#欢迎 关`：关闭欢迎。
3. `#欢迎 词 你的欢迎词`：修改欢迎词模板。
4. `#盲盒统计`：查询你本月盲盒汇总（兼容 `#盲盒 我的` / `#盲盒我的`）。

## 礼物感谢自定义

配置项：`features.thanks_template`

可用占位符：`{uname}`、`{gift_name}`、`{gift_num}`。

示例：`@{uname} 感谢你送出的 {gift_name} x{gift_num}！`