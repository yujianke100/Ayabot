"""权限矩阵：把全部路由按角色打一遍，断言「谁都不该拿到额外权限」。

第 19/20 轮修过一次大范围 IDOR（24 个房间接口无鉴权、streamer 角色全权限），
但当时的验证是**手工挑了几个接口**。这里枚举路由表，逐条断言：

  1. 路径带房间号且不在自己名下        -> 必须 403（任何方法）
  2. 全局写操作不在白名单              -> 必须 403
  3. 全局密钥类 GET（响应里有明文 key） -> 必须 403
  4. 自己房间的只读接口                -> 必须不是 403
     （例外：`/imports`、`/stats_check` 是**有意**只给管理员的管理工具，
      界面上「数据管理」标签对普通账号也是隐藏的）
  5. 未知角色（界面写进去的 `streamer`）与 `user` 权限完全相同 ——
     以前 `if role == "user":` 让 streamer 直接落到 else 全权限。

    ./.venv/Scripts/python.exe scripts/check_acl_matrix.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 18967
MY_ROOM = "1000000001"
OTHER_ROOM = "1000000002"
USER_TOKEN = "u-token"
STREAMER_TOKEN = "s-token"

#: 有意只给管理员的管理工具（界面对普通账号隐藏），GET 也应当 403
ADMIN_ONLY_GET = {"/imports", "/stats_check"}

#: 普通账号允许的写操作
ALLOWED_WRITE = {
    (f"/api/rooms/{MY_ROOM}/start", "POST"),
    (f"/api/rooms/{MY_ROOM}/stop", "POST"),
    (f"/api/rooms/{MY_ROOM}/restart", "POST"),
    (f"/api/rooms/{MY_ROOM}/config", "POST"),
    ("/api/user/password", "POST"),
    # 注销自己的会话：必须对所有登录用户开放，否则普通账号永远退不出去
    ("/api/logout", "POST"),
}

#: 会把测试用的会话干掉，所以放到最后一步单独验，不能混在其他断言中间跑
DESTRUCTIVE_WRITE = {("/api/logout", "POST")}


def main() -> int:
    import uvicorn
    from fastapi.routing import APIRoute
    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="acl_"))
    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        if not cond:
            print(f"  FAIL {label}" + (f" -> {extra}" if extra else ""))
            fails.append(label)

    try:
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (tmp / "data").mkdir(parents=True)
        (tmp / "config.yaml").write_text(example, encoding="utf-8")
        for rid in (MY_ROOM, OTHER_ROOM):
            d = tmp / "rooms" / rid
            (d / "data").mkdir(parents=True)
            (d / "config.yaml").write_text(example, encoding="utf-8")

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._API_TOKEN = "t"
        now = time.time()
        srv._SESSIONS[USER_TOKEN] = (now + 3600, "alice", "user", [MY_ROOM])
        srv._SESSIONS[STREAMER_TOKEN] = (now + 3600, "bob", "streamer", [MY_ROOM])
        srv._rate_limited = lambda ip: False  # type: ignore[assignment]
        srv._GIFT_CACHE_BUILT = True
        srv._GIFT_PRICE_CACHE = {}

        cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="critical")
        server = uvicorn.Server(cfg)
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(120):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)

        routes: list[tuple[str, str]] = []
        for r in srv.app.routes:
            if isinstance(r, APIRoute):
                for m in sorted(r.methods):
                    if m not in ("HEAD", "OPTIONS"):
                        routes.append((m, r.path))
        routes.sort()

        def call(method: str, path: str, token: str) -> int:
            data = b"{}" if method in ("POST", "PUT", "PATCH") else None
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method)
            req.add_header("Cookie", f"session={token}")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                return exc.code
            except Exception:  # noqa: BLE001
                return 0

        def concrete(path: str, room: str) -> str:
            return (path.replace("{room_id}", room)
                        .replace("{uid}", "1")
                        .replace("{name}", "vue.js")
                        .replace("{username}", "someone"))

        print("[1] 别人的房间：任何方法都必须 403")
        n = 0
        for method, path in routes:
            if "{room_id}" not in path:
                continue
            p = concrete(path, OTHER_ROOM)
            code = call(method, p, USER_TOKEN)
            ok(f"{method} {p}", code == 403, f"HTTP {code}")
            n += 1
        print(f"  检查 {n} 条")

        print("[2] 全局写操作：除白名单外必须 403")
        n = 0
        for method, path in routes:
            if method not in ("POST", "PUT", "PATCH", "DELETE"):
                continue
            if "{room_id}" in path or path == "/api/login":
                continue
            p = concrete(path, MY_ROOM)
            if (p, method) in ALLOWED_WRITE or p in {a for a, _ in ALLOWED_WRITE}:
                continue
            code = call(method, p, USER_TOKEN)
            ok(f"{method} {p}", code == 403, f"HTTP {code}")
            n += 1
        print(f"  检查 {n} 条")

        print("[3] 全局密钥接口：必须 403")
        for path in sorted(srv._SECRET_PATHS) + sorted(srv._ADMIN_ONLY_PREFIXES):
            code = call("GET", path, USER_TOKEN)
            ok(f"GET {path}", code == 403, f"HTTP {code}")

        print("[4] 自己房间的只读接口：不能是 403（管理工具除外）")
        for method, path in routes:
            if method != "GET" or "{room_id}" not in path:
                continue
            suffix = "/" + path.rsplit("/", 1)[-1]
            if suffix in ADMIN_ONLY_GET:
                code = call("GET", concrete(path, MY_ROOM), USER_TOKEN)
                ok(f"GET {path} 应为管理员专属", code == 403, f"HTTP {code}")
                continue
            p = concrete(path, MY_ROOM)
            code = call(method, p, USER_TOKEN)
            ok(f"GET {p}", code != 403, f"HTTP {code}")

        print("[5] 白名单写操作确实放行（不是靠 403 蒙过去）")
        for path, method in sorted(ALLOWED_WRITE - DESTRUCTIVE_WRITE):
            p = path if "{room_id}" not in path else path.replace("{room_id}", MY_ROOM)
            code = call(method, p, USER_TOKEN)
            ok(f"{method} {p} 放行", code != 403, f"HTTP {code}")

        print("[6] 未知角色（streamer）与 user 完全同权")
        diff = []
        dead = {p for p, _ in DESTRUCTIVE_WRITE}
        for method, path in routes:
            # 注销接口会把调用者的会话干掉，跑到这里就等于把两个测试 token 都注销了
            if path in dead or (path, method) in DESTRUCTIVE_WRITE:
                continue
            p = concrete(path, OTHER_ROOM)
            a, b = call(method, p, USER_TOKEN), call(method, p, STREAMER_TOKEN)
            if (a == 403) != (b == 403):
                diff.append((method, p, a, b))
        ok("streamer 没有额外权限", not diff, str(diff[:4]))

        print("[7] 注销接口：普通账号能调用，且之后自己立刻失效")
        code = call("POST", "/api/logout", STREAMER_TOKEN)
        ok("POST /api/logout 不是 403", code != 403, f"HTTP {code}")
        code_after = call("GET", f"/api/rooms/{OTHER_ROOM}/config", STREAMER_TOKEN)
        ok("注销后 streamer 的 token 失效（401）", code_after == 401, f"HTTP {code_after}")
        # user 的会话没被池鱼之殃
        code_user = call("GET", f"/api/rooms/{MY_ROOM}/config", USER_TOKEN)
        ok("另一个账号的会话不受影响", code_user != 401, f"HTTP {code_user}")

        server.should_exit = True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项（前 12 条）：")
        for f in fails[:12]:
            print("  -", f)
        return 1
    print("ACL MATRIX OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
