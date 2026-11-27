"""刷新页面 / 退出登录 必须真的对（HttpOnly cookie 的两个坑）。

session cookie 是 **httponly** 的，JS 用 `document.cookie` 看不到它。前端原来
用 `document.cookie.includes('session=')` 猜「是否已登录」，于是：

1. **每按一次 F5 都要求重新登录** —— 服务端会话明明还有效（`/api/rooms` 返回
   200），界面却停在登录框。用户感受是「打开后台就要重新登录」。
2. **「退出登录」从来没生效** —— `doLogout` 只写了
   `document.cookie = 'session=;max-age=0'`，而这行代码对 HttpOnly cookie
   完全无效，服务端那个 token 一直有效到过期。

这里用一个真服务 + 真 token 验证：
  A. 登录后 `/api/me` 能拿到身份（前端靠它恢复登录态）
  B. `/api/logout` 之后**同一个 token 立刻失效**（401）
  C. 没登录时 `/api/me` 是 401（不能把用户信息漏给匿名请求）
  D. 普通账号也能注销（不被「写操作默认拒绝」拦掉）

    ./.venv/Scripts/python.exe scripts/check_session_restore.py
"""
from __future__ import annotations

import http.cookiejar
import json
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

PORT = 19023


def main() -> int:
    import uvicorn
    from app.web import server as srv

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="sessrestore_"))
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    try:
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (tmp / "data").mkdir(parents=True)
        (tmp / "config.yaml").write_text(example, encoding="utf-8")
        room = tmp / "rooms" / "1946287911"
        (room / "data").mkdir(parents=True)
        (room / "config.yaml").write_text(example, encoding="utf-8")

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._API_TOKEN = ""
        srv._rate_limited = lambda ip: False  # type: ignore[assignment]
        srv._login_limited = lambda ip, user="": False  # type: ignore[assignment]

        # 造两个账号：管理员 + 普通用户（普通用户只分到一个房间）
        from app.web import server as _s
        users = _s._load_users()
        users["t_admin"] = {"password_hash": _s._hash_password("admin-pass-1"),
                            "role": "admin", "allowed_rooms": []}
        users["t_user"] = {"password_hash": _s._hash_password("user-pass-1"),
                           "role": "streamer", "allowed_rooms": ["1946287911"]}
        _s._save_users(users)

        cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="critical")
        server = uvicorn.Server(cfg)
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(120):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)

        def call(path: str, method="GET", body=None) -> tuple[int, dict]:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}{path}", data=data, method=method,
                headers={"Content-Type": "application/json"})
            try:
                with opener.open(req, timeout=20) as r:
                    raw = r.read().decode("utf-8", "replace")
                    try:
                        return r.status, json.loads(raw)
                    except Exception:  # noqa: BLE001
                        return r.status, {"_raw": raw[:120]}
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                try:
                    return exc.code, json.loads(raw)
                except Exception:  # noqa: BLE001
                    return exc.code, {"_raw": raw[:120]}

        print("[1] 未登录时 /api/me 必须是 401")
        code, body = call("/api/me")
        ok("HTTP 401", code == 401, f"{code} {body}")
        ok("没有把任何用户名漏出去", "username" not in body, body)

        print("[2] 登录后 /api/me 能拿到身份（前端靠它恢复登录态）")
        code, body = call("/api/login", "POST",
                          {"username": "t_admin", "password": "admin-pass-1"})
        ok("登录成功", code == 200, f"{code} {body}")
        code, me = call("/api/me")
        ok("/api/me 200", code == 200, code)
        ok("username 正确", me.get("username") == "t_admin", me.get("username"))
        ok("role 正确", me.get("role") == "admin", me.get("role"))
        ok("带 must_reset_password 字段", "must_reset_password" in me, list(me))
        ok("cookie 是 httponly（所以前端不能靠 document.cookie 判断）",
           any(c.name == "session" and c.has_nonstandard_attr("HttpOnly") for c in jar),
           [(c.name, c.has_nonstandard_attr("HttpOnly")) for c in jar])
        ok("/api/rooms 此时可用", call("/api/rooms")[0] == 200)

        print("[3] 注销之后同一个 token 立刻失效")
        token_before = next((c.value for c in jar if c.name == "session"), "")
        code, body = call("/api/logout", "POST")
        ok("注销返回 200", code == 200, f"{code} {body}")
        ok("服务端会话表里已经没有它", token_before not in srv._SESSIONS,
           f"token={token_before[:8]}… 仍存在" if token_before in srv._SESSIONS else "")
        code, body = call("/api/me")
        ok("注销后 /api/me 是 401", code == 401, code)
        code, body = call("/api/rooms")
        ok("注销后 /api/rooms 是 401", code == 401, code)

        print("[4] 普通账号（streamer）也能注销，不被「写操作默认拒绝」拦")
        jar.clear()
        code, body = call("/api/login", "POST",
                          {"username": "t_user", "password": "user-pass-1"})
        ok("普通账号登录成功", code == 200, f"{code} {body}")
        code, me = call("/api/me")
        ok("普通账号 /api/me 正常", code == 200 and me.get("username") == "t_user",
           f"{code} {me}")
        code, body = call("/api/logout", "POST")
        ok("普通账号注销不是 403", code != 403, f"{code} {body}")
        code, body = call("/api/me")
        ok("注销后确实没了会话", code == 401, code)

        print("[5] 伪造/过期的 token 不能换到身份")
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/me",
                                     headers={"Cookie": "session=deadbeef"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                code = r.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        ok("/api/me 拒绝伪造 token", code == 401, code)

        print("[6] 滑动过期仍然生效（注销接口不能把它弄坏）")
        jar.clear()
        call("/api/login", "POST", {"username": "t_admin", "password": "admin-pass-1"})
        tok = next((c.value for c in jar if c.name == "session"), "")
        ok("登录后拿到了 token", bool(tok), tok[:8] if tok else "")
        # 把它改成「快过期」，只有真的推后了才说明滑动生效（>= 那种断言等于没测）
        exp, user, role, allowed = srv._SESSIONS[tok]
        near = time.time() + 30
        srv._SESSIONS[tok] = (near, user, role, allowed)
        srv._touch_session(tok)
        after = srv._SESSIONS[tok][0]
        ok("活跃后过期时间被推后（不是原地不动）", after > near + 60,
           f"{int(near)} -> {int(after)}")
        code, body = call("/api/rooms")
        ok("滑动之后请求仍然正常", code == 200, code)

        server.should_exit = True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("SESSION RESTORE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
