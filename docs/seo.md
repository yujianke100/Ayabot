# 搜索引擎收录（SEO）

官网：<https://ayabot.jianke-yu.online>

## 2026-10-20 实测现状

| 搜索引擎 | `site:ayabot.jianke-yu.online` | 搜 `ayabot` | 结论 |
|---|---|---|---|
| 百度 | 「抱歉，未找到相关结果」 | 第 3 条是**知乎文章**（《主播必备！Ayabot：B站直播机器人开箱即用》，2026-06-19），官网不在结果里 | 官网未收录；但百度已经知道 "Ayabot" 这个词 |
| Bing | 无关结果 | 无关结果 | 未收录 |
| Google | 需要人机验证，脚本查不到 | 同左 | 未确认（要靠 Search Console） |

服务器 nginx 日志里**没有任何真实爬虫**访问过 ayabot 站点（只有扫描器和我们自己的浏览器）。
原因很直接：新域名、没有任何外部链接指向它、也没有 sitemap/提交过收录。

## 已经做好的（不需要你操作）

1. **robots.txt**（`/robots.txt`）：允许全站抓取，声明 sitemap
2. **sitemap.xml**（`/sitemap.xml`）：收录入口
3. **页面 SEO**
   - `<title>` 带关键词：`Ayabot · B站直播间机器人 | 免费开源的弹幕/礼物/盲盒/上舰统计工具`
   - `description` 完整一句话介绍 + 免费 + 本地 SQLite
   - `keywords`（百度仍会略读）
   - `canonical` + `og:url`，避免 http/https 重复收录
   - **JSON-LD 结构化数据**（`SoftwareApplication`）：名称、免费、GPL-3.0、下载地址、Windows/Linux，
     搜索结果的富摘要会用到
4. **IndexNow 提交**（Bing / Yandex / Seznam 共用接口，无需账号）
   - 关键文件：`/indexnow-0c11cf819a38666f7584134a2a211b0e.txt`
   - 已提交 → `api.indexnow.org` 与 `www.bing.com/indexnow` 都返回 **HTTP 202（已接受）**
5. **外链（爬虫发现站点的入口）**
   - GitHub 仓库 Homepage 已设为 `https://ayabot.jianke-yu.online`
   - GitHub 仓库 topics 已加：bilibili / live-streaming / danmaku / bot / gift-tracker / ai-chatbot / python / fastapi / windows-exe
   - Gitee 仓库 Homepage 与简介也已加上官网地址
6. 技术面检查过：三大爬虫 UA（Baiduspider / Googlebot / bingbot）访问首页都是 **200 + 完整 HTML**；
   `http://` 是 **301 → https**；未知路径是 **404**（不会伪造首页）

## 需要你操作的三件事

这三步都绑定账号，我做不了（也不该拿你的账号）。

### 1. 百度（对国内用户最重要）

1. 打开 <https://ziyuan.baidu.com> → 「用户中心 → 站点管理 → 添加网站」
2. 站点填 `https://ayabot.jianke-yu.online`，验证方式选 **CNAME 验证**（最省事）
   - 或者用「HTML 标签验证」，把给的 `<meta>` 贴进 `site/index.html` 的 `<head>`，再跑 `bash site/deploy.sh`
3. 验证通过后 → 「普通收录 → API 提交」拿到 token，存下来并推送：

```bash
printf '%s' '你的token' > ~/.baidu-push-token
python scripts/baidu_push.py
```

返回 `成功推送 1 条` 就说明百度已经在排队抓取。配额约 10 条/天，本站只有首页要推。

> 备案：域名已备案（站点跑在腾讯云上海），这是百度收录的前提，不用额外处理。

### 2. Google

<https://search.google.com/search-console> → 添加「网址前缀」`https://ayabot.jianke-yu.online`
→ 用 **DNS TXT 验证**（在域名解析里加一条 TXT，最省事）→ 提交 `https://ayabot.jianke-yu.online/sitemap.xml`。

### 3. Bing 站长工具

<https://www.bing.com/webmasters> → 添加站点 → 可以直接「从 Google Search Console 导入」（Google 那步做完后一键导入）。
IndexNow 已经在推，导入主要是为了看抓取报告和收录量。

## 见效周期（真实预期）

- **Bing**：IndexNow 提交后通常 1–3 天出现；Bing 对新站比较友好
- **Google**：几天到两周；第一次抓取后会先以 `site:` 查得到
- **百度**：新站最慢，常见 **1–4 周**，而且需要外链；只提交 sitemap 往往不够，主动推送 + 外链一起上

## 想更快被搜到：三条外链（都有现成的落点）

1. **B站视频简介**：BV17vje6NE5z（《主播必备！Ayabot…》）的简介里加一句
   「官网：https://ayabot.jianke-yu.online」。B站内容百度抓得很勤，这是最有效的国内外链。
2. **知乎文章**：那篇《主播必备！Ayabot：B站直播机器人开箱即用》已被百度收录，
   在里面补上官网链接（并更新发布时间），百度会重新抓取并跟到官网。
3. **GitHub README**：已经写了官网地址；README 被 Google 高频抓取，正常情况下几天内
   Google 就能顺着链接发现站点。

## 复查命令

```bash
# 收录查询（在浏览器里手动查更准，脚本会被反爬拦）
# 百度:  site:ayabot.jianke-yu.online
# Bing:  site:ayabot.jianke-yu.online
# Google:site:ayabot.jianke-yu.online

# 检查爬虫真来过没有（服务器上）
ssh cloud "sudo grep -i 'baiduspider\|googlebot\|bingbot' /var/log/nginx/access.log | grep -v 127.0.0.1 | tail"
```

## 踩过的坑

- **别信「HTTP 200 就算通过」**：urllib/curl 默认跟随 301，`http://` 会看起来是 200，
  实际是 301 跳转到 https。要判断得看 `Location` 头。
- **`www.ayabot.jianke-yu.online` 的证书是不过的**：`*.jianke-yu.online` 只覆盖一级子域，
  `www.ayabot.…` 是二级。不要再对外宣传 www 前缀，canonical 用的是裸域名。
- **IndexNow 需要 key 文件可访问**：先把 `indexnow-<key>.txt` 传上去再 POST，顺序反了会 403。
