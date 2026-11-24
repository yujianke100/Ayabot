"""中间件常量 vs 真实路由表 —— 自动对账。

中间件里那些常量（免登录白名单、密钥接口名单、体积上限、房间写操作白名单）
都写在路由定义**之外**。路由一改名，这些常量就静默失效，而且不会报错：

    path.endswith("/danmaku/import")   vs   真实 /api/rooms/{id}/import/danmaku-xml
    → 导入接口拿到全局 8MB 上限，模块自己的 64MB 校验根本没机会跑

这条自检把每一类常量都拿去和 `app.routes` 反查，任何「指向不存在的路由」
都算失败。这样以后再改路由名，这条断言会立刻红，而不是等用户报障。

    ./.venv/Scripts/python.exe scripts/check_middleware_routes.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from fastapi.routing import APIRoute
    from app.web import server as srv

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    # 路由表：把 {xxx} 换成具体值，方便和白名单里的字面量比对
    literal: set[str] = set()          # 没有路径参数的路由
    templated: list[str] = []
    for r in srv.app.routes:
        if not isinstance(r, APIRoute):
            continue
        p = r.path
        if "{" in p:
            templated.append(p)
        else:
            literal.add(p)

    def route_exists(path: str) -> bool:
        """path 是否对应一条真实路由（容忍路径参数）。"""
        if path in literal:
            return True
        for t in templated:
            pat = "^" + re.sub(r"\{[^/]+\}", "[^/]+", t) + "$"
            if re.match(pat, path):
                return True
        return False

    def any_route_matches(pred) -> str:
        for t in templated + sorted(literal):
            if pred(t):
                return t
        return ""

    print("[1] 免登录白名单：每条都必须真的是个路由")
    for p in sorted(srv._PUBLIC_EXTERNAL_GETS):
        ok(f"GET {p}", route_exists(p))

    print("[2] 密钥接口名单：每条都必须真的是个路由")
    for p in sorted(srv._SECRET_PATHS):
        ok(f"{p}", route_exists(p))
    for p in sorted(srv._ADMIN_ONLY_PREFIXES):
        ok(f"{p}", any(rt.startswith(p) for rt in literal | set(templated)),
           f"无任何路由以 {p} 开头")

    print("[3] 普通账号的房间写操作白名单：后缀必须真的有对应路由")
    room_suffixes = {
        rt[len("/api/rooms/{room_id}"):].rstrip("/")
        for rt in templated if rt.startswith("/api/rooms/{room_id}")
    }
    for suffix in sorted(srv._USER_ROOM_MUTATIONS):
        ok(f"POST /api/rooms/{{id}}{suffix}", suffix in room_suffixes, f"现有后缀: {sorted(room_suffixes)}")

    print("[4] 全局写操作白名单")
    for p in sorted(srv._USER_GLOBAL_MUTATIONS):
        ok(p, route_exists(p))

    print("[5] 体积上限：XML 导入正则必须命中真实路由")
    real_import = [rt for rt in templated if rt.endswith("/import/danmaku-xml")]
    ok("路由表里存在 import/danmaku-xml", bool(real_import), str(real_import))
    for rt in real_import:
        concrete = rt.replace("{room_id}", "1946287911")
        ok(f"上限正则匹配 {rt}", bool(srv._XML_IMPORT_PATH_RE.match(concrete)),
           f"正则={srv._XML_IMPORT_PATH_RE.pattern}")
    # 反向：不能误伤普通接口
    ok("上限正则不匹配 /api/rooms/x/ranking",
       not srv._XML_IMPORT_PATH_RE.match("/api/rooms/1/ranking"))

    print("[6] 房间路径正则必须命中所有房间子路由")
    bad = [rt for rt in templated if rt.startswith("/api/rooms/")
           and not srv._ROOM_PATH_RE.match(rt.replace("{room_id}", "1946287911"))]
    ok("所有 /api/rooms/* 都被 _ROOM_PATH_RE 覆盖", not bad, str(bad[:4]))

    print("[7] 账号路径正则")
    bad2 = [rt for rt in templated if rt.startswith("/api/bili_accounts/")
            and not srv._ACCOUNT_PATH_RE.match(rt.replace("{uid}", "123")) ]
    ok("所有 /api/bili_accounts/* 都被 _ACCOUNT_PATH_RE 覆盖", not bad2, str(bad2[:4]))

    print("[8] 中间件里不应该再出现「字面量后缀」判断")
    src = (ROOT / "app/web/server.py").read_text(encoding="utf-8")
    # 只检查中间件那一段
    start = src.index("async def _auth_middleware")
    end = src.index("# ══════════════════════════════════════════════════════════════════\n#  Auth API")
    seg = src[start:end]
    stale = re.findall(r'path\.endswith\("(/[^"]+)"\)', seg)
    ok("中间件不再用 path.endswith(字面量) 判定路由", not stale, str(stale))

    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("MIDDLEWARE ROUTES OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
