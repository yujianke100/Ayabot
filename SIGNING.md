# Code signing policy

本项目（Ayabot）发布的 Windows 可执行文件使用代码签名，签名方式见下。

## Windows — SignPath Foundation（申请中）

**Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by [SignPath Foundation](https://signpath.org/)**

- 签名的对象：GitHub Releases 上发布的 `Ayabot-v<版本>.exe` 与 `Ayabot-latest.exe`
- 只签名由本仓库 CI（GitHub Actions）构建出来的产物；手工构建或第三方转发的二进制不签名
- 私钥由 SignPath 保管（HSM），本项目不持有、也无法导出私钥
- 签名前 SignPath 会校验产物的来源（确实由本仓库的 workflow 构建），每个版本需由 Approver 明确批准

申请状态：`pending`（批准后此节会改为 `active`，并附上首个已签名版本号）

在批准之前，CI 发布的产物未签名，Windows 首次运行会提示「无法验证发布者」；
程序启动时会自动清除自身下载标记（`Zone.Identifier`），因此只有每次升级后的第一次启动会提示一次。

## 团队角色

单维护者项目：

- **Authors**（可直接改仓库、无需额外评审）：
  [@yujianke100](https://github.com/yujianke100)
- **Reviewers**（外部提交的 PR 必须经评审后合并）：[@yujianke100](https://github.com/yujianke100)
- **Approvers**（每次签名请求的批准人）：[@yujianke100](https://github.com/yujianke100)

所有成员对 GitHub 与 SignPath 账号均启用双因素认证（MFA）。

## 构建与签名流程

1. 推送 `v*` tag 触发 `.github/workflows/build-exe.yml`
2. GitHub Actions（GitHub 托管 runner，`windows-latest`）用 PyInstaller 构建 exe，
   版本资源写入 `ProductName` / `ProductVersion`
3. 未签名产物先上传为 workflow artifact，再发布到 Release（保证签名等待期间用户可下载）
4. 配置了 SignPath 时，CI 提交签名请求 → Approver 在 SignPath 批准 → 签名产物覆盖 Release 同名资产

## 隐私政策

本程序不会向用户未指定之外的网络系统传输信息。

需要说明的是：Ayabot 是一个直播间机器人，为实现其功能会把用户主动配置的内容
（直播间号、B 站登录凭证、弹幕/礼物事件、用户自行配置的大模型 API 地址与密钥）
发送给用户自己指定的服务——B 站开放接口与用户自己填写的 LLM 服务。
所有配置项都在本地 `config.yaml` / 管理后台中由用户填写，不包含任何预置的第三方上报端点。

## 分发位置

- GitHub Releases：https://github.com/yujianke100/Ayabot/releases

## 其他签名情况

- macOS / Linux：暂不提供签名产物，用户应从上述 Releases 页获取
