#!/usr/bin/env bash
# 多连接分片下载 GitHub Release 资产，并用 sha256 校验。
#
# 为什么需要它：国内单连接从 GitHub 加速站下 38MB 只有 10–25 KB/s（要半小时以上），
# 而 gh-proxy 之类的加速站对同一资产允许并发 Range，8 连接实测能到 400–500 KB/s（约 5 分钟）。
#
# 踩过的坑：
#   1. 分片重试时**不能**让 curl 覆盖已下的前缀，必须把剩余段下到临时文件再追加；
#      否则文件大小对得上、内容却是错的（sha256 才发现）。
#   2. 临时文件必须**按分片分开**，共用会被并发写乱。
#   3. 必须校验响应码是 206：服务器忽略 Range 会返回 200 + 整个文件，直接追加就全乱了。
#
# 用法:
#   ./fetch_asset.sh <url> <输出文件> <期望字节数> <期望sha256|-> [分片数]
set -uo pipefail

URL="$1"; OUT="$2"; SIZE="$3"; SHA="${4:--}"; PARTS="${5:-8}"
[ "$SHA" = "-" ] && SHA=""

TMP="${OUT}.parts"
rm -rf "$TMP"; mkdir -p "$TMP"
CHUNK=$(( SIZE / PARTS + 1 ))

fetch_part() {
  local i="$1" s=$(( $1 * CHUNK ))
  local e=$(( s + CHUNK - 1 ))
  [ "$e" -ge "$SIZE" ] && e=$(( SIZE - 1 ))
  local want=$(( e - s + 1 ))
  local part="$TMP/p$(printf '%03d' "$i")"
  local tail_f="$TMP/.tail.$i"
  : > "$part"
  local attempt have code
  for attempt in 1 2 3 4 5 6 7 8; do
    have=$(stat -c %s "$part" 2>/dev/null || echo 0)
    [ "$have" -ge "$want" ] && break
    rm -f "$tail_f"
    code=$(curl -sL --max-time 300 -r "$(( s + have ))-$e" -o "$tail_f" -w '%{http_code}' "$URL" 2>/dev/null || echo 000)
    if [ "$code" != "206" ]; then
      echo "分片 $i: Range 不被支持（HTTP $code）" >&2
      rm -f "$tail_f"; return 1
    fi
    cat "$tail_f" >> "$part"
    rm -f "$tail_f"
    sleep 1
  done
  have=$(stat -c %s "$part" 2>/dev/null || echo 0)
  if [ "$have" -gt "$want" ]; then
    head -c "$want" "$part" > "$part.trim" && mv "$part.trim" "$part"
  fi
  have=$(stat -c %s "$part" 2>/dev/null || echo 0)
  [ "$have" -eq "$want" ]
}

pids=(); fail=0
for i in $(seq 0 $(( PARTS - 1 ))); do
  fetch_part "$i" & pids+=($!)
done
for p in "${pids[@]}"; do wait "$p" || fail=1; done
if [ "$fail" != 0 ]; then
  echo "分片失败，改用单连接重试（服务器可能不支持 Range）"
  rm -rf "$TMP"
  curl -fL --retry 5 -C - --max-time 3600 -o "$OUT" "$URL" || exit 1
else
  : > "$OUT"
  for f in "$TMP"/p[0-9]*[0-9]; do cat "$f" >> "$OUT"; done
  rm -rf "$TMP"
fi

got=$(stat -c %s "$OUT")
if [ "$got" != "$SIZE" ]; then echo "大小不符: $got != $SIZE"; exit 1; fi
if [ -n "$SHA" ]; then
  actual=$(sha256sum "$OUT" | cut -d' ' -f1)
  if [ "$actual" != "$SHA" ]; then
    echo "sha256 不符，文件不可信"
    echo "  期望 $SHA"
    echo "  实际 $actual"
    exit 1
  fi
  echo "sha256 校验通过"
fi
echo "OK $OUT $got bytes"
