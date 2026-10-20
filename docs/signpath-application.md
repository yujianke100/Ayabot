# SignPath Foundation 申请表（照填）

申请页：https://signpath.org/apply 　条件：https://signpath.org/terms.html

表单是 HubSpot 嵌入的，带 hCaptcha，**必须你本人提交**（涉及身份、邮箱、验证码）。
下面每一栏给的都是可直接粘贴的内容。

---

## 表单字段

| 字段 | 填什么 |
|---|---|
| **Project Name**\* | `Ayabot` |
| **Repository URL**\* | `https://github.com/yujianke100/Ayabot` |
| **Homepage URL**\* | `https://github.com/yujianke100/Ayabot` |
| **Download URL** | `https://github.com/yujianke100/Ayabot/releases` |
| **Privacy Policy URL** | `https://github.com/yujianke100/Ayabot/blob/main/SIGNING.md#隐私政策` |
| **Wikipedia URL** | 留空 |
| **Tagline**\* | `Bilibili live-stream chatbot with per-room gift/blindbox/guard statistics` |
| **Category** | `Developer Tools` 或 `Utilities`（下拉里挑最接近的） |
| **Maintainer type** | `Individual` |
| **Build system** | `GitHub Actions`（下拉里是 GitHub 就选 GitHub） |
| **First name / Last name / Email** | 你自己的，用能收邮件的地址 |
| **Company Name** | 留空 |
| **Primary Discovery Channel** | 下拉里随便挑一个真实的，比如 `Search engine`；后面的 `Please specify…` 写 `GitHub / Bilibili community` |
| **Exact license type** | `GPL-3.0-only (GNU General Public License v3.0 only)` |

勾选框：Code of Conduct（同意遵守）、接收 SignPath 邮件、同意数据处理 —— 三个都勾。

## Description\*（原样粘贴）

```
Ayabot is a Windows desktop application (single-file .exe) for Bilibili live-stream
chatbot automation. Streamers who run a Bilibili live room use it to answer danmaku
(viewer chat) with keyword rules or an LLM, send welcome/thank-you messages for gifts,
and keep local per-room statistics (gifts, blind boxes, guard/subscription purchases,
leaderboards, CSV export).

It is a fully open-source project under GPL-3.0-only. The Python source, the PyInstaller
build script and the GitHub Actions workflow that produces every release are all in the
public repository. Users run it entirely on their own machine: it stores data in a local
SQLite file under %LOCALAPPDATA%\Ayabot and talks only to (a) Bilibili's public/authenticated
live APIs, which the user themselves logs into by QR code, and (b) an LLM endpoint that the
user configures with their own API key (or none at all). There is no telemetry and no
vendor-operated backend.

Artifacts we ask to be signed: the Windows executables published on GitHub Releases
(Ayabot-v<version>.exe and Ayabot-latest.exe). They are PyInstaller one-file builds made by
the GitHub Actions workflow on GitHub-hosted windows-latest runners, for every v* tag.
The user-facing reason we need signing is Windows SmartScreen/"unknown publisher"
prompts: our users are mostly non-technical streamers in China, and the warning makes them
abandon installation or download the binary from untrusted third-party re-uploaders.

Note on our user base: this is a small niche project (11 stars, ~630 release downloads over
~20 releases, actively maintained since April 2026). We are applying to the Foundation
programme precisely because we cannot afford a commercial certificate as an individual
developer.
```

## Reputation\*（原样粘贴，按实际情况微调数字）

```
Public repository since 2026-04-27, actively maintained (commits and releases through 2026-10),
21 tagged releases, ~630 downloads of the Windows binary tracked by the GitHub Releases API,
11 stars / 1 fork. Issues and pull requests are handled in the public tracker. The project
has never been reported for malware or bundled installers; the binary is a single self-contained
exe built in public CI from the tagged commit, and every release's source is available at that tag.

Hmm — I am aware that our download volume is modest. If verifiable reputation below a certain
threshold disqualifies us, please tell us what would be needed and we will come back; alternatively
we are happy to start with the artifacts being built only by GitHub-hosted runners with
manual approval on every release (which we already do).
```

> 最后这段是说人话的表态 —— 如果你不想显得"求人"，删掉 `Hmm —` 那整段也行，
> 保留前三句事实就够。**任何数字变了就按 Releases 页的真实数据改**，别让它看起来像模板。

---

## 提交后会发生什么

1. 邮件往来确认信息（可能问项目用途、用户量、构建方式）→ 通常几天到两三周
2. 批准后拿到 SignPath 组织 → 建 Project（slug 必须是 `ayabot`）+ Signing Policy（slug 必须是 `release-signing`），
   因为 CI 里已经写死这两个 slug（`.github/workflows/build-exe.yml`）
3. 装 SignPath GitHub App 到本仓库
4. 生成 API token → 在仓库 Settings → Secrets 里加 `SIGNPATH_API_TOKEN`、`SIGNPATH_ORG_ID`
5. 打下一个 tag，CI 会自动提交签名请求并在 SignPath 后台等你点「批准」

## 待办（批准后 5 分钟能完成）

- [ ] SignPath 后台建 project `ayabot`
- [ ] 建 signing policy `release-signing`（manual approval，Approver = 你自己）
- [ ] 建 artifact configuration：根节点 `<zip>` → `<pe-file>` 匹配 `*.exe`，
      设 metadata restrictions：ProductName = `Ayabot 直播间机器人`，ProductVersion = 参数 `version`
- [ ] 安装 SignPath GitHub App（仓库 Code + Actions: read）
- [ ] Secrets：`SIGNPATH_API_TOKEN`、`SIGNPATH_ORG_ID`
- [ ] 打 tag 验证：`Get-AuthenticodeSignature` 应显示 Valid / `CN=SignPath Foundation`
- [ ] 把 SIGNING.md 里的 `申请状态：pending` 改成 `active` + 首个签名版本号
