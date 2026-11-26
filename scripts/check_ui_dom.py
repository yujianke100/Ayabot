"""真浏览器 UI 回归（包一层 node scripts/check_ui_dom.js）。

后端自检再全，也拦不住这些纯前端故障 —— 而它们恰恰是用户最先看到的：
v-cloak 白屏、凭证失效却不引导扫码、崩溃提示没有重试时间、日志面板执行注入、
换房间看到上一个房间的数据。以前这些断言散在临时目录的 puppeteer 脚本里，
被 24h 清理删掉了，等于最有价值的回归测试从没进过仓库。

这里额外做一层静态检查：四个「房间级」加载函数都必须有迟到响应的保护，
不然跑得再绿也只是运气好（本机快，响应没迟到）。

    ./.venv/Scripts/python.exe scripts/check_ui_dom.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "app" / "web" / "server.py"


def static_checks() -> list[str]:
    """房间级加载函数必须忽略「切走之后才回来的」响应。"""
    src = SERVER.read_text(encoding="utf-8")
    fails: list[str] = []
    # 每个函数：抓函数体（到下一个同缩进的 def / 函数结尾）
    for fn in ("loadBotLog", "loadDanmakuLog", "loadGuards", "loadRanking"):
        m = re.search(rf"async function {fn}\(\s*\)\s*\{{", src)
        if not m:
            fails.append(f"{fn}: 找不到函数（改名了？检查脚本要跟着改）")
            continue
        body = src[m.end():m.end() + 4000]
        # 截到下一个顶层函数定义
        nxt = re.search(r"\n        (?:async )?function ", body)
        if nxt:
            body = body[:nxt.start()]
        if "_roomStillCurrent(roomId)" not in body:
            fails.append(f"{fn}: 没有 _roomStillCurrent 保护，切房间会显示上一个房间的数据")
        if "await fetch" not in body:
            fails.append(f"{fn}: 函数体看起来不对（没找到 await fetch）")
    if "function _roomStillCurrent" not in src:
        fails.append("_roomStillCurrent 帮助函数被删了")
    return fails


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP：没装 node，跳过真浏览器 UI 回归")
        return 0

    fails = static_checks()
    for f in fails:
        print(f"  FAIL {f}")

    js = ROOT / "scripts" / "check_ui_dom.js"
    if not js.exists():
        print("  FAIL scripts/check_ui_dom.js 不见了")
        return 1

    proc = subprocess.run([node, str(js)], cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=420)
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.rstrip())

    if "SKIP" in out.splitlines()[0] if out.splitlines() else False:
        return 0
    if proc.returncode != 0:
        return 1
    if fails:
        print(f"\nUI DOM 静态检查 FAILED {len(fails)} 项")
        return 1
    print("\nUI DOM OK（含静态保护检查）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
