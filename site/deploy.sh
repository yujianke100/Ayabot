#!/usr/bin/env bash
# 把 site/ 部署到腾讯云服务器（ayabot.jianke-yu.online）
#
# 服务器上的结构（第一次部署已经手工配好，这里只做内容同步）：
#   /home/homepage/ayabot                      ← 静态根目录（nginx 以 homepage 用户读取）
#   /usr/local/nginx/conf/nginx.conf           ← stream 里 SNI 分流 ayabot.* -> 127.0.0.1:6444
#                                                 http 里 listen 6444 ssl 的 vhost
#
# 用法: bash site/deploy.sh
set -euo pipefail

HOST="${AYABOT_SITE_HOST:-cloud}"          # ~/.ssh/config 里的 Host 别名
DEST="/home/homepage/ayabot"
SRC="$(cd "$(dirname "$0")" && pwd)"

# 版本号与安装包大小写进 version.json（页面用它显示「最新 vX.Y.Z」）
# 优先按「线上实际最新的 Release」取值，避免主页写着还没发布的版本号；
# 取不到（GitHub 不通）时退回仓库里的 VERSION。
api='https://api.github.com/repos/yujianke100/Ayabot/releases/latest'
meta="$(curl -sL --max-time 15 "https://gh-proxy.com/$api" 2>/dev/null || true)"
ver="$(printf '%s' "$meta" | grep -o '"tag_name"[^,]*' | head -1 | sed 's/.*"v\?\([0-9][^"]*\)".*/\1/')"
size_bytes="$(printf '%s' "$meta" | tr -d '\n' | sed -n 's/.*"size": *\([0-9][0-9]*\).*/\1/p')"
if [ -z "$ver" ]; then
  ver="$(sed -n '1p' "$SRC/../VERSION" | tr -d '\r' | sed 's/^\xef\xbb\xbf//' | tr -d '[:space:]')"
  echo "（取不到线上版本，退回本地 VERSION=$ver）"
fi
size_mb="$(awk -v b="${size_bytes:-0}" 'BEGIN{printf "%.1f", b/1048576}')"

cat > "$SRC/version.json" <<EOF
{
 "version": "$ver",
 "size_mb": $size_mb,
 "updated_at": "$(date +%Y-%m-%d)"
}
EOF
echo "version.json -> v$ver, ${size_mb} MB"

ssh "$HOST" "mkdir -p $DEST/assets"
scp "$SRC/index.html" "$SRC/version.json" "$SRC/404.html" "$SRC/site.webmanifest" \
    "$SRC/robots.txt" "$SRC/sitemap.xml" "$HOST:$DEST/"
# IndexNow 验证文件（Bing / Yandex 靠它确认域名归属），有就一起传
if ls "$SRC"/indexnow-*.txt >/dev/null 2>&1; then
  scp "$SRC"/indexnow-*.txt "$HOST:$DEST/"
fi
scp "$SRC"/assets/* "$HOST:$DEST/assets/"
# 根目录也放一份 favicon.ico：浏览器在没解析到 <link> 时会直接请求 /favicon.ico
scp "$SRC"/assets/favicon.ico "$HOST:$DEST/favicon.ico"
ssh "$HOST" "chown -R homepage:homepage $DEST && ls -l $DEST $DEST/assets | head -24"

# 静态文件改动不需要 reload nginx（root 未变）；只有改 vhost 才需要
#
# 注意：主机带宽要留给别的项目，官网不托管 exe。下载一律走 GitHub Release 与各家加速站。
echo "done -> https://ayabot.jianke-yu.online/"
