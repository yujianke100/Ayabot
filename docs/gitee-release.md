# Gitee 国内下载线路（由 GitHub Actions 自动发布）

国内直连 GitHub Release 经常失败，第三方加速站时好时坏。Gitee 的**发行版附件**走它自己的国内 CDN
（`foruda.gitee.com`），实测约 2 MB/s，且**不占用任何自建服务器的带宽**。

CI 会在打 tag 后自动把 exe 传到 Gitee（`.github/workflows/build-exe.yml` 的 `gitee` job），
用的是 `scripts/publish_gitee.py`。没配置 token 时这个 job 直接跳过，不影响发版。

## 一次性配置（约 5 分钟）

### 1. 建 Gitee 仓库

1. 登录 <https://gitee.com>，右上角 `+` → **新建仓库**
2. 仓库名填 `Ayabot`，路径也填 `Ayabot`（路径要和 CI 里的 owner/repo 一致）
3. 勾选「使用 Readme 文件初始化这个仓库」——**必须有至少一个提交**，否则无法创建发行版
4. 公开仓库即可（社区版配额只对公开仓库友好）

> 想顺手镜像源码：仓库页「管理」→「仓库镜像管理」→ 添加 Pull 方向镜像指向
> `https://github.com/yujianke100/Ayabot.git`。注意 Gitee 的自动镜像长期受 GitHub 侧限制，
> 拉不动也没关系——**下载线路只依赖发行版附件，不依赖代码镜像**。

### 2. 建个人访问令牌

<https://gitee.com/profile/personal_access_tokens> → 生成新令牌：

- 名称：`ayabot-ci`
- 权限勾选 **projects**（仓库读写）即可，其它可全选或不选
- 生成后立刻复制（只显示一次）

### 3. 配到 GitHub 仓库

仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| Secret | 值 | 必填 |
|---|---|---|
| `GITEE_TOKEN` | 上一步的令牌 | ✅ |
| `GITEE_OWNER` | 你的 Gitee 用户名/个性地址，例如 `yujianke100` | 建议 |
| `GITEE_REPO` | 仓库路径，例如 `Ayabot` | 建议 |
| `GITEE_TARGET` | 发行版指向的分支，Gitee 上要存在，通常是 `master` | 建议 |

不填 owner/repo 时脚本默认用 `yujianke100/Ayabot`。

### 4. 验证

打一个 patch 版本（或直接重跑 workflow）后，看 GitHub Actions 里 `gitee` job 的日志：

```
发布到 Gitee: yujianke100/Ayabot v1.2.3
  创建发行版 v1.2.3 (id=xxxx)
  上传 Ayabot-latest.exe (38.0 MB) ...
维护 latest 别名发行版（固定下载地址）
  已上传 (id=xxxx)
完成：https://gitee.com/yujianke100/Ayabot/releases/download/v1.2.3/Ayabot-latest.exe
```

然后命令行验证（应返回 302 → `foruda.gitee.com`）：

```bash
curl -sI "https://gitee.com/yujianke100/Ayabot/releases/download/latest/Ayabot-latest.exe" | head -3
```

## 下载地址（发布后长期稳定）

| 地址 | 用途 |
|---|---|
| `https://gitee.com/<owner>/<repo>/releases/download/latest/Ayabot-latest.exe` | **固定地址**，永远指向最新版（官网用这个） |
| `https://gitee.com/<owner>/<repo>/releases/download/v<版本>/Ayabot-latest.exe` | 指定版本 |

`latest` 是一个额外的发行版：每次发版都会把它的附件替换成最新的 exe，所以地址不变。
App 内的更新检查用 `v<版本>` 那条（它已经知道版本号），官网用 `latest`。

## 配额与轮换

社区版限制（<https://help.gitee.com/account/usage-quota>）：

- 单个附件 ≤ **100 MB** —— 我们的 exe 约 38 MB，安全
- 单仓库附件总量 ≤ **1 G** —— 约 26 个版本，所以脚本会轮换：
  `--keep 4` 表示只保留最近 4 个版本发行版的附件，更早的附件自动删除（发行版本身保留）

`latest` 别名不参与轮换计数，正常占用约 38 MB。

## 说明与坑

- **Gitee Pages 不能用来放官网**：2024 年它已全面停止服务，别在这上面花时间。官网仍放在自己的服务器上（纯静态，几十 KB）。
- **匿名可下载**：`releases/download/...` 无需登录，302 到 CDN 的签名地址，链接带时效 token 但入口地址永久有效。
- **附件名固定为 `Ayabot-latest.exe`**：官网/脚本/App 都按这个名字拼地址，改名要三处同步（`scripts/publish_gitee.py`、`app/web/server.py` 的 `_UPDATE_ASSET`、`site/index.html` 的线路表）。
- **CI 里 `gh` 必须显式给 `GH_TOKEN`**，否则静默失败（本项目踩过）。
- 附件上传是 `multipart/form-data`，字段名固定 `file`，`access_token` 也走表单字段而不是 header。
- 同一发行版内附件不能重名：脚本先 `DELETE` 同名附件再上传。

## 本地手动发布（可选）

```bash
export GITEE_TOKEN=xxxx
python scripts/publish_gitee.py --version 1.2.3 --exe dist/Ayabot-v1.2.3.exe \
    --notes-file RELEASE.md --owner yujianke100 --repo Ayabot
```
