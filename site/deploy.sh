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
ver="$(sed -n '1p' "$SRC/../VERSION" | tr -d '\r' | sed 's/^\xef\xbb\xbf//' | tr -d '[:space:]')"
size_bytes="$(curl -sIL --max-time 20 \
  https://github.com/yujianke100/Ayabot/releases/latest/download/Ayabot-latest.exe \
  | tr -d '\r' | awk 'tolower($1)=="content-length:"{v=$2} END{print v}')"
size_mb="$(python3 -c "print(round(${size_bytes:-0}/1048576,1))" 2>/dev/null || echo 0)"

cat > "$SRC/version.json" <<EOF
{
 "version": "$ver",
 "size_mb": $size_mb,
 "updated_at": "$(date +%Y-%m-%d)"
}
EOF
echo "version.json -> v$ver, ${size_mb} MB"

ssh "$HOST" "mkdir -p $DEST"
scp "$SRC/index.html" "$SRC/version.json" "$HOST:$DEST/"
ssh "$HOST" "chown -R homepage:homepage $DEST && ls -l $DEST"

# 静态文件改动不需要 reload nginx（root 未变）；只有改 vhost 才需要
echo "done -> https://ayabot.jianke-yu.online/"
