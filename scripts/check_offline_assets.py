"""校验 WebUI 的前端资源全部本地内置（不依赖任何外网 CDN）。

背景：v1.2.5 及之前 vue / chart.js / tailwind / modern-screenshot 都从 unpkg、jsdelivr 拉。
国内网络连不上 unpkg 时 Vue 不会挂载，用户看到的是一整页 `{{ }}` 裸模板（v-if 也不生效），
但机器人后台其实在正常跑 —— 于是就出现「界面全乱、机器人却是好的」这种反馈。

本脚本检查四件事：
  1. 模板里没有任何直接外链 CDN 的 <script src="http...">（只允许兜底 document.write 里出现）；
  2. app/web/vendor/ 下四个库都存在且体积正常、版本与预期一致；
  3. /vendor/* 在鉴权中间件的白名单里（否则登录页自己也加载不到 Vue）；
  4. 真起一个 uvicorn，在未登录状态下 GET / 和 /vendor/*，必须 200 且内容正确。

用法: ./.venv/Scripts/python.exe scripts/check_offline_assets.py
"""
from __future__ import annotations

import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 19897

# 文件 -> (最小字节数, 必须出现的特征串)
VENDOR = {
    "vue.global.prod.js": (150_000, ("3.5.", "createApp")),
    "chart.umd.min.js": (150_000, ("4.6", "Chart")),
    "tailwind.js": (300_000, ()),
    "modern-screenshot.js": (20_000, ("modernScreenshot",)),
}


def _get(path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    fails: list[str] = []

    # ── 1. 模板不得再直接外链 CDN ──
    src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
    m = re.search(r'INDEX_HTML\s*=\s*r"""(.*?)"""', src, re.S)
    if not m:
        print("FAILED: 提取不到 INDEX_HTML 模板")
        return 1
    html = m.group(1)

    external = re.findall(r'<script[^>]+src="(https?://[^"]+)"', html)
    print(f"外链 <script src>: {external if external else '无'}")
    if external:
        fails.append(f"模板仍在直接外链 CDN: {external}")

    for bad in ("cdn.tailwindcss.com", "unpkg.com/vue", "cdn.jsdelivr.net/npm/chart.js"):
        # 只允许出现在兜底 document.write 里
        if bad in html and "document.write" not in html:
            fails.append(f"{bad} 仍被直接引用")

    for tag in ("/vendor/vue.global.prod.js", "/vendor/chart.umd.min.js",
                "/vendor/tailwind.js", "/vendor/modern-screenshot.js"):
        ok = tag in html
        print(f"  {'OK ' if ok else 'MISS'} 模板引用 {tag}")
        if not ok:
            fails.append(f"模板未引用 {tag}")

    # ── 2. 本地库文件 ──
    vdir = ROOT / "app" / "web" / "vendor"
    for name, (min_size, needles) in VENDOR.items():
        p = vdir / name
        if not p.exists():
            print(f"  MISS 文件 {name}")
            fails.append(f"缺少 {name}")
            continue
        size = p.stat().st_size
        text = p.read_text(encoding="utf-8", errors="ignore") if size < 3_000_000 else ""
        miss = [n for n in needles if n not in text]
        ok = size >= min_size and not miss
        print(f"  {'OK  ' if ok else 'MISS'} 文件 {name:24} {size:>9,}B"
              + (f"  缺特征 {miss}" if miss else ""))
        if not ok:
            fails.append(f"{name} 体积异常或版本不符")

    # ── 3. 鉴权白名单 ──
    allow = 'path.startswith("/vendor/")' in src
    print(f"  {'OK ' if allow else 'MISS'} 鉴权白名单含 /vendor/（登录页也要加载 Vue）")
    if not allow:
        fails.append("/vendor/ 不在 _auth_middleware 白名单")

    # ── 4. 真实 HTTP ──
    import uvicorn

    from app.web import server as srv

    config = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(80):
        if getattr(server, "started", False):
            break
        time.sleep(0.2)
    if not getattr(server, "started", False):
        print("FAILED: 测试服务没起来")
        return 1

    try:
        code, body = _get("/")
        print(f"  {'OK ' if code == 200 else 'MISS'} GET / -> {code} ({len(body):,}B)")
        if code != 200:
            fails.append("GET / 失败")
        if b'id="bootfail"' not in body:
            fails.append("模板缺少 bootfail 兜底提示")
        if b"v-cloak" not in body:
            fails.append("模板缺少 v-cloak（会在挂载前闪出裸模板）")

        for name, (min_size, _n) in VENDOR.items():
            code, body = _get(f"/vendor/{name}?v=test")
            ok = code == 200 and len(body) >= min_size
            print(f"  {'OK ' if ok else 'MISS'} 未登录 GET /vendor/{name} -> {code} "
                  f"({len(body):,}B)")
            if not ok:
                fails.append(f"/vendor/{name} 未登录拿不到（登录页会白屏）")

        code, _ = _get("/vendor/../server.py")
        ok = code in (403, 404)
        print(f"  {'OK ' if ok else 'MISS'} 目录穿越 GET /vendor/../server.py -> {code}")
        if not ok:
            fails.append("vendor 路由存在目录穿越风险")
    finally:
        server.should_exit = True
        th.join(timeout=8)

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(" -", f)
        return 1
    print("\nOFFLINE ASSETS OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
