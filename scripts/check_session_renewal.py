"""登录会话的滑动过期：活跃使用不该被踢下线。

`_SESSION_TIMEOUT`（默认 3600 秒）以前是**登录时算死的**到期时间，从不续期。
后果：用户连续操作满 1 小时（编辑配置、导入 XML、看统计…）会在某次请求上
突然被判 401 → 前端 `loggedIn.value = false` → 跳回登录页，
**正在编辑的表单全部丢失**，而用户完全不知道发生了什么。

两层都要滑动，缺一不可：
  1. 服务端 `_SESSIONS` 里的到期时间（`_touch_session`）
  2. 浏览器里的 Cookie `max_age`（中间件按 `_COOKIE_REFRESH_EVERY` 节流重发）
只做第一层的话，浏览器到点照删 Cookie，用户照样被踢。

    ./.venv/Scripts/python.exe scripts/check_session_renewal.py
"""
from __future__ import annotations

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

PORT = 18997
ROOM = "1946287911"
TOKEN = "sess-renew"


def main() -> int:
    import uvicorn
    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="sessrenew_"))
    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    try:
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (tmp / "data").mkdir(parents=True)
        (tmp / "config.yaml").write_text(example, encoding="utf-8")
        (tmp / "rooms" / ROOM / "data").mkdir(parents=True)
        (tmp / "rooms" / ROOM / "config.yaml").write_text(example, encoding="utf-8")

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._API_TOKEN = TOKEN
        srv._SESSIONS[TOKEN] = (time.time() + 3600, "probe", "admin", [])
        srv._SESSION_COOKIE_AT.clear()
        srv._rate_limited = lambda ip: False  # type: ignore[assignment]

        cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="critical")
        server = uvicorn.Server(cfg)
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(120):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)

        def call(path: str = "/api/rooms") -> tuple[int, list[str]]:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                         headers={"Cookie": f"session={TOKEN}"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, [v for k, v in r.headers.items()
                                      if k.lower() == "set-cookie"]
            except urllib.error.HTTPError as exc:
                return exc.code, []

        print("[1] 正常会话可用")
        code, _ = call()
        ok("HTTP 200", code == 200, code)

        print("[2] 服务端滑动过期")
        srv._SESSIONS[TOKEN] = (time.time() + 300, "probe", "admin", [])
        exp0 = srv._SESSIONS[TOKEN][0]
        call()
        exp1 = srv._SESSIONS[TOKEN][0]
        ok("活跃请求把到期时间往后推", exp1 > exp0 + 60,
           f"{int(exp1 - exp0)}s 后延")
        ok("推到接近满额（不是只推一点点）", exp1 - time.time() > 3000,
           f"剩余 {int(exp1 - time.time())}s")

        print("[3] 浏览器 Cookie 也要续发")
        srv._SESSION_COOKIE_AT.clear()
        code, cookies = call()
        ok("响应里有 Set-Cookie", bool(cookies), cookies[:1])
        ok("带 session=", any("session=" in c for c in cookies), cookies[:1])
        ok("带 max_age", any("Max-Age" in c or "max-age" in c for c in cookies), cookies[:1])
        ok("是 httponly", any("HttpOnly" in c or "httponly" in c for c in cookies), cookies[:1])

        print("[4] 节流：同一秒内连续请求不会每次都 Set-Cookie")
        srv._SESSION_COOKIE_AT.clear()
        call()
        _, cookies2 = call()
        ok("第二次不再重发（避免每个请求都刷 Cookie）", not cookies2, cookies2[:1])

        print("[5] 真过期仍然要 401（滑动不等于永不过期）")
        srv._SESSIONS[TOKEN] = (time.time() - 1, "probe", "admin", [])
        code, _ = call()
        ok("过期后 401", code == 401, code)
        ok("过期 session 被清掉", TOKEN not in srv._SESSIONS)
        ok("Cookie 续发记录也清掉", TOKEN not in srv._SESSION_COOKIE_AT)

        print("[6] 配置层拦掉会让人被秒踢的值")
        from app.config import ConfigError, load_config
        import yaml
        base = yaml.safe_load(example)
        for val, should_fail in ((0, True), (10, True), ("一小时", True), (3600, False)):
            raw = yaml.safe_load(yaml.dump(base))
            raw.setdefault("web_ui", {})["session_timeout"] = val
            p = tmp / f"st_{abs(hash(str(val)))}.yaml"
            p.write_text(yaml.dump(raw, allow_unicode=True), encoding="utf-8")
            try:
                load_config(str(p))
                got = False
            except ConfigError:
                got = True
            ok(f"session_timeout={val!r} {'被拦' if should_fail else '放行'}",
               got is should_fail, f"got={got}")

        print("[7] 结构：中间件确实在改 response 的 cookie")
        src = (ROOT / "app/web/server.py").read_text(encoding="utf-8")
        ok("_touch_session 存在", "def _touch_session(" in src)
        ok("_check_auth 里调用续期", "_touch_session(token)" in src)
        ok("中间件拿到 response 后再 set_cookie", "response.set_cookie(" in src)
        ok("续发有节流", "_COOKIE_REFRESH_EVERY" in src)

        server.should_exit = True
    finally:
        srv._SESSION_COOKIE_AT.clear()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("SESSION RENEWAL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
