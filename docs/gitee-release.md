# Gitee 国内下载线路

国内直连 GitHub Release 经常失败，第三方加速站时好时坏。Gitee 的**发行版附件**走它自己的国内 CDN
（`foruda.gitee.com`），实测约 2 MB/s，且**不占用任何自建服务器的带宽**。

## 为什么不用 jsDelivr

实测 + 查文档，两条硬限制，走不通：

1. **单文件上限 20 MB**（GitHub 源）。官方文档原文：
   > Packages larger than 150 MB or single files larger than 20 MB (in the case of GitHub) are not supported by default.
   —— <https://www.jsdelivr.com/documentation#id-Publishing-packages>

   exe 约 **38 MB**，直接超限。
2. **jsDelivr 只服务「仓库里提交过的文件」**，不是 Release 附件。要让它发 exe，就得把 38 MB
   二进制提交进 git 并打 tag —— 每版仓库 +38 MB 且永久留在历史里（GitHub 单文件还有 100 MB 上限），
   对源码仓库是很差的实践。

另外本机（中国移动杭州家宽）实测：`cdn.jsdelivr.net` 直接连不上（curl `code=000`），只有
`fastly.jsdelivr.net` / `gcore.jsdelivr.net` 通（1.2–1.7 s）。国内对主域名的可达性本来就不稳定，
不适合当主下载线路。

> jsDelivr 官方有「custom CDN hosting」可托管二进制（Windows 可执行文件），但需要发邮件申请
> <d@jsdelivr.com>，不该作为当前方案——Gitee 现成可用。

## 谁上传（关键：不占服务器带宽）

| 方式 | 能否用 | 耗时 | 说明 |
|---|---|---|---|
| **GitHub Actions 直接传** | ❌ | 10 分钟起还不成功 | runner 在境外，向 gitee.com 发两个小 GET 就花 10 分钟，传 38 MB 直接 `write operation timed out` |
| **本机 / 任意国内电脑（推荐）** | ✅ | 约 20–60 秒 | 只消耗本机上行 38 MB/版 |
| **国内服务器（不采用）** | ✅ 但不该用 | 约 20 秒 | 会吃掉服务器的出网流量，用户明确要求留给别的项目 |

`build-exe.yml` 的 `gitee` job 默认**关闭**（要试就把仓库变量 `GITEE_CI_UPLOAD` 设为 `1`），
避免每次发版都卡十分钟再报红。

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

### 3. 把令牌放到本机（不要让令牌进 git）

```bash
# 本机执行一次
printf '%s' '你的Gitee令牌' > ~/.gitee-token
chmod 600 ~/.gitee-token
```

脚本的令牌优先级：`--token` 参数 > `GITEE_TOKEN` 环境变量 > `~/.gitee-token`。

### 4. 验证「令牌和权限对不对」（不传大文件）

```bash
python scripts/publish_gitee.py --probe
```

会建一个临时发行版、传 1 KB、读回确认、再删掉；输出 `PROBE OK` 即代表通。

## 每次发版后：两条命令

```bash
# 1) 从 GitHub Release 拉刚发布的 exe（走加速站，不占自己的带宽）
curl -fL -o /tmp/ayabot.exe \
  https://gh-proxy.com/https://github.com/yujianke100/Ayabot/releases/download/v1.2.3/Ayabot-latest.exe

# 2) 传到 Gitee（约 20–60 秒）
python scripts/publish_gitee.py --version 1.2.3 --exe /tmp/ayabot.exe
```

脚本会：建/复用 `v<版本>` 发行版 → 上传 exe → 把 `latest` 别名发行版的附件换成这一版 →
按 `--keep 4` 轮换旧版本附件。全程只需要 Gitee token，不碰自有服务器。

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

## 官网是怎么用它的

官网的线路列表不是写死的「谁最快」，而是：

1. 先用 API 确认这条线路**真的有文件**：GitHub 走 `api.github.com/.../releases/latest`，
   Gitee 走 `gitee.com/api/v5/repos/<owner>/<repo>/releases/tags/latest`（两者都带
   `Access-Control-Allow-Origin: *`，浏览器能直接读状态）
2. 再用 `mode:'no-cors'` 发 HEAD 计时（**不能**用 cors 模式测 Gitee：它的 302 跳转响应
   没带 ACAO，浏览器会直接拒读，表现为「Failed to fetch」）
3. 只给「确认存在」的线路计时，404 的线路标「未启用」，避免 404 因为快而被推荐

所以 Gitee 仓库没建好之前，官网上它会显示「未启用 / 未发布」——这是预期行为，建好后自动变成最快那条。

## 说明与坑

- **Gitee Pages 不能用来放官网**：2024 年它已全面停止服务，别在这上面花时间。官网仍放在自己的服务器上（纯静态，几十 KB）。
- **匿名可下载**：`releases/download/...` 无需登录，302 到 CDN 的签名地址，链接带时效 token 但入口地址永久有效。
- **附件名固定为 `Ayabot-latest.exe`**：官网/脚本/App 都按这个名字拼地址，改名要三处同步（`scripts/publish_gitee.py`、`app/web/server.py` 的 `_UPDATE_ASSET`、`site/index.html` 的线路表）。
- **CI 里 `gh` 必须显式给 `GH_TOKEN`**，否则静默失败（本项目踩过）。
- 附件上传是 `multipart/form-data`，字段名固定 `file`，`access_token` 也走表单字段而不是 header。
- 同一发行版内附件不能重名：脚本先 `DELETE` 同名附件再上传。

## 想用 CI 上传时（可选）

仓库 Settings → Secrets 加 `GITEE_TOKEN`（`GITEE_OWNER` / `GITEE_REPO` / `GITEE_TARGET` 可选），
再把仓库 **Variables** `GITEE_CI_UPLOAD` 设为 `1`，tag 发版时才会跑 `gitee` job。
另外可以手动触发「Gitee 补发」工作流（`gitee-republish.yml`），勾 `probe` 只做连通性验证。

两条路都受同一个网络问题限制：runner 在境外，成功率取决于当时 Gitee 的国际线路。

