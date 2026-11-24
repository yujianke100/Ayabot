"""前端调用的接口 vs 后端真实路由 —— 逐条对账。

前端（内嵌在 server.py 的 `INDEX_HTML`）里所有 `fetch('...')` / `axios` 调用的路径，
必须在 `app.routes` 里真实存在。反过来，任何「点了没反应」「静默 404」的按钮
都是这么来的：前端还在请求一个后端已经改名/删除的接口。

同时反过来查：**写接口**（POST/PUT/PATCH/DELETE）如果在路由表里存在，
但前端从未调用，那它要么是给脚本用的、要么是死代码 —— 后者会积累成
「没人测的接口」，所以列出来供人工判断（不算失败）。

    ./.venv/Scripts/python.exe scripts/check_frontend_routes.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: 前端里拼接出来的路径片段，这些不需要精确匹配
DYNAMIC_MARKERS = ("${", "+", "?")


def main() -> int:
    from fastapi.routing import APIRoute
    from app.web import server as srv

    fails: list[str] = []
    src = (ROOT / "app/web/server.py").read_text(encoding="utf-8")

    # 只取前端那部分（INDEX_HTML 起），避免把后端自己的 urllib 请求算进来
    try:
        html_start = src.index("INDEX_HTML")
    except ValueError:
        print("FAILED: 找不到 INDEX_HTML")
        return 1
    html = src[html_start:]
    html = html.replace("\\`", "`")

    calls: set[str] = set()
    for m in re.finditer(r"""fetch\(\s*(`[^`]*`|'[^']*'|"[^"]*")""", html):
        raw = m.group(1)[1:-1]
        calls.add(raw)
    for m in re.finditer(r"""url:\s*(`[^`]*`|'[^']*'|"[^"]*")""", html):
        calls.add(m.group(1)[1:-1])

    routes = []
    for r in srv.app.routes:
        if isinstance(r, APIRoute):
            for method in r.methods:
                if method not in ("HEAD", "OPTIONS"):
                    routes.append((method, r.path))

    route_pats = {re.sub(r"\{[^}]+\}", "{x}", rt).rstrip("/") or "/" for _m, rt in routes}

    def matches(call: str) -> bool:
        """把前端路径模板化后和后端路由比对。

        前端常见写法是 `.../danmaku-xml${qs}`（把查询串拼进了一个变量），
        去掉 `?` 之后末尾还留着一个 `{x}`。所以除了整体比对，
        还要试「把末尾那个模板段砍掉」的版本。
        """
        pat = re.sub(r"\$\{[^}]*\}", "{x}", call)
        pat = pat.split("?")[0].rstrip("/") or "/"
        candidates = {pat}
        if pat.endswith("{x}"):
            trimmed = pat[: -len("{x}")].rstrip("/") or "/"
            candidates.add(trimmed)
        return any(c in route_pats for c in candidates)

    print(f"前端共引用 {len(calls)} 个请求地址")
    print("-" * 70)
    checked = 0
    dead: list[str] = []
    for call in sorted(calls):
        if not call.startswith("/api") and not call.startswith("/"):
            continue
        if any(m in call for m in DYNAMIC_MARKERS) and "${" not in call:
            continue  # 字符串拼接出来的，静态无法判定
        checked += 1
        if not matches(call):
            dead.append(call)
            print(f"  FAIL  前端请求了不存在的接口: {call}")
            fails.append(call)
        else:
            print(f"  OK    {call}")
    print(f"  可静态校验的 {checked} 条")

    print()
    print("[2] 后端有、前端从不调用的写接口（可能是死代码，也可能是给脚本/外部用的）")
    called_norm = {re.sub(r"\$\{[^}]*\}", "{x}", c).split("?")[0].rstrip("/") for c in calls}
    external_only = []
    for method, path in sorted(set(routes)):
        if method not in ("POST", "PUT", "PATCH", "DELETE"):
            continue
        rp = re.sub(r"\{[^}]+\}", "{x}", path).rstrip("/")
        if rp in called_norm or path in called_norm:
            continue
        external_only.append((method, path))
    for method, path in external_only:
        print(f"  -  {method:6s} {path}")
    print(f"  共 {len(external_only)} 条（仅供人工判断，不计失败）")

    print()
    if fails:
        print(f"FAILED {len(fails)} 项：前端在请求不存在的接口")
        return 1
    print("FRONTEND ROUTES OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
