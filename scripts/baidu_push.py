"""把站点 URL 主动推送给百度（需要「普通收录」里的推送 token）。

为什么需要单独推：百度不会主动发现一个新域名，主动推送是它明确支持、且当天就能生效的通道。

用法:
    1) 在 https://ziyuan.baidu.com 用百度账号登录，添加站点 https://ayabot.jianke-yu.online
       （推荐用「CNAME 验证」或「HTML 标签验证」，之后把 token 记下来）
    2) 进「普通收录 → API 提交」，复制那串 token
    3) 把 token 存起来（只需一次）：
         printf '%s' '你的token' > ~/.baidu-push-token
    4) 推送：
         python scripts/baidu_push.py
         python scripts/baidu_push.py --url https://ayabot.jianke-yu.online/sitemap.xml

配额：普通站点每天大约 10 条，够用（本站只有首页要推）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SITE = "https://ayabot.jianke-yu.online"
DEFAULT_URLS = [SITE + "/"]
TOKEN_FILE = Path.home() / ".baidu-push-token"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="append", default=[], help="要推送的 URL，可重复")
    ap.add_argument("--token", default="", help="百度推送 token（默认读 ~/.baidu-push-token）")
    ap.add_argument("--site", default=SITE, help="站点地址（需与站长平台里登记的完全一致）")
    args = ap.parse_args()

    token = (args.token or "").strip()
    if not token:
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            print(f"缺少 token：请先执行 printf '%s' '你的token' > {TOKEN_FILE}")
            print("token 在 https://ziyuan.baidu.com → 普通收录 → API 提交")
            return 2
    urls = args.url or DEFAULT_URLS

    api = f"http://data.zz.baidu.com/urls?site={urllib.parse.quote(args.site, safe='')}&token={token}"
    body = "\n".join(urls).encode("utf-8")
    req = urllib.request.Request(
        api, data=body, method="POST",
        headers={"Content-Type": "text/plain", "User-Agent": "Ayabot-seo"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        print("推送失败:", exc)
        return 1

    print(f"HTTP {status}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(raw[:300])
        return 1

    if data.get("success"):
        print(f"成功推送 {data['success']} 条，今日剩余配额 {data.get('remain', '?')} 条")
        return 0
    print("未成功:", json.dumps(data, ensure_ascii=False))
    # 常见错误：token 无效 / 站点未验证 / 配额用尽 / URL 不属于该站点
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
