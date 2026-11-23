"""校验角色收紧、全局密钥脱敏、图片代理加固（第二十轮）。

本轮背景（都是上一轮「修完」之后仍然真实可利用的问题）：

1. `streamer` 角色绕过全部限制
   中间件里写的是 `if role == "user":`，而 WebUI 的「新增主播账号」表单
   写入的 role 正好是 `"streamer"`。于是**每个从界面建出来的主播账号**
   都不等于 admin、也不等于 user，直接落到 `return await call_next(request)`，
   上一轮加的 24 个房间接口 ACL 对它们完全不生效（可删任意房间）。

2. 全局 LLM 密钥对普通账号可见
   `/api/general_config`、`/api/llm_config`、`/api/llm_config/export`、
   `/api/templates` 的响应里带**明文** api_key；房间 config.yaml 抄了全局
   llm 段，主播读自己房间的配置就能拿到管理员的密钥。

3. `/api/proxy_image` 是免登录的通用代理
   实测 `/api/proxy_image?url=http://127.0.0.1:8799/internal` 会把本机
   任意 HTTP 服务的响应原样返回（SSRF + 开放代理），而且 `resp.read()`
   不限长度，一个超大响应就能把进程撑爆。

4. 礼物图标缓存在启动时网络抖一下就永久缺失（`_GIFT_CACHE_BUILT` 置 True）。

用法: ./.venv/Scripts/python.exe scripts/check_role_secrets_proxy.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 19894
ROOM_A = "1900000333"
ROOM_B = "1900000444"
TOK_ADMIN = "tok-admin-20"
TOK_STREAMER = "tok-streamer-20"   # WebUI 建出来的主播账号角色就是 streamer
TOK_USER = "tok-user-20"
FAKE_KEY = "sk-SECRET-do-not-leak-2024"


def _req(method: str, path: str, token: str | None) -> tuple[int, str]:
    headers = {"Cookie": f"session={token}"} if token else {}
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", method=method, headers=headers,
        data=b"{}" if method in ("POST", "PUT", "DELETE") else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    import uvicorn
    import yaml

    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_role_"))
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    example["llm"] = dict(example.get("llm") or {}, api_key=FAKE_KEY, provider="openai")
    for rid in (ROOM_A, ROOM_B):
        d = tmp / "rooms" / rid
        (d / "data").mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(
            yaml.safe_dump(example, allow_unicode=True, sort_keys=False), encoding="utf-8")

    srv._ROOMS_BASE_DIR = str(tmp)
    srv._ROOM_DB_PATH_CACHE.clear()
    srv._rate_limited = lambda ip: False  # type: ignore[assignment]
    exp = time.time() + 900
    srv._SESSIONS[TOK_ADMIN] = (exp, "root", "admin", [])
    srv._SESSIONS[TOK_STREAMER] = (exp, "anchor", "streamer", [ROOM_A])
    srv._SESSIONS[TOK_USER] = (exp, "plain", "user", [ROOM_A])

    async def _fake_ok(rid):  # noqa: ANN001
        return True

    srv.start_room_async = _fake_ok        # type: ignore[assignment]
    srv.stop_room_async = _fake_ok         # type: ignore[assignment]
    srv.restart_room_async = _fake_ok      # type: ignore[assignment]
    srv.room_status = lambda rid: "stopped"  # type: ignore[assignment]
    srv.clean_room = lambda rid: None      # type: ignore[assignment]

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    server = uvicorn.Server(uvicorn.Config(srv.app, host="127.0.0.1", port=PORT,
                                          log_level="critical"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(80):
        if server.started:
            break
        time.sleep(0.25)
    if not server.started:
        print("FAILED: uvicorn 未启动")
        return 1

    try:
        # ── 1. streamer 角色必须和 user 一样受限 ──
        print("[1] streamer（界面建出来的主播账号）不能再操作别人的房间")
        for method, path, label in (
            ("GET", f"/api/rooms/{ROOM_B}/ranking", "读排行"),
            ("GET", f"/api/rooms/{ROOM_B}/config", "读配置"),
            ("POST", f"/api/rooms/{ROOM_B}/stop", "停机器人"),
            ("POST", f"/api/rooms/{ROOM_B}/delete_old", "删历史数据"),
            ("DELETE", f"/api/rooms/{ROOM_B}", "删除房间"),
        ):
            code, body = _req(method, path, TOK_STREAMER)
            ok(f"streamer {label} -> 403", code == 403, f"{code} {body[:60]}")
        ok("房间 B 的配置文件还在",
           (tmp / "rooms" / ROOM_B / "config.yaml").exists())

        print("[2] streamer 仍然能管自己的房间")
        code, body = _req("GET", f"/api/rooms/{ROOM_A}/ranking", TOK_STREAMER)
        ok("streamer 读自己房间排行 -> 200", code == 200, f"{code}")
        code, body = _req("POST", f"/api/rooms/{ROOM_A}/start", TOK_STREAMER)
        ok("streamer 启自己房间 -> 200", code == 200, f"{code}")
        code, body = _req("POST", f"/api/rooms/{ROOM_A}/stop", TOK_STREAMER)
        ok("streamer 停自己房间 -> 200", code == 200, f"{code}")

        # ── 2. 全局密钥接口 ──
        print("[3] 普通账号/主播账号读不到明文密钥")
        for tok, who in ((TOK_USER, "user"), (TOK_STREAMER, "streamer")):
            for path, label in (
                ("/api/general_config", "全局配置"),
                ("/api/llm_config", "LLM 配置"),
                ("/api/llm_config/export", "LLM 导出"),
                ("/api/templates", "预设模板"),
            ):
                code, body = _req("GET", path, tok)
                ok(f"{who} 读{label} -> 403", code == 403, f"{code}")
                ok(f"{who} 读{label} 不含密钥", FAKE_KEY not in body)
        code, body = _req("GET", "/api/general_config", TOK_ADMIN)
        ok("admin 读全局配置 -> 200", code == 200, f"{code}")

        print("[4] 房间配置里的 llm.api_key 对非管理员脱敏")
        code, body = _req("GET", f"/api/rooms/{ROOM_A}/config", TOK_STREAMER)
        ok("streamer 读自己房间配置 -> 200", code == 200, f"{code}")
        ok("响应里没有明文密钥", FAKE_KEY not in body, body[:80])
        ok("密钥位置是掩码", "********" in body)
        code, body = _req("GET", f"/api/rooms/{ROOM_A}/config/export", TOK_STREAMER)
        ok("导出配置也没有明文密钥", code == 200 and FAKE_KEY not in body, f"{code}")
        code, body = _req("GET", f"/api/rooms/{ROOM_A}/config", TOK_ADMIN)
        ok("admin 仍能看到真实密钥", code == 200 and FAKE_KEY in body, f"{code}")

        # ── 3. 图片代理 ──
        print("[5] /api/proxy_image 必须登录 + 只能代理 B站图片域名")
        code, body = _req("GET", "/api/proxy_image?url=http://i0.hdslb.com/a.png", None)
        ok("未登录 -> 401", code == 401, f"{code} {body[:60]}")

        for url, label in (
            ("http://127.0.0.1:8799/internal", "直连本机端口"),
            ("http://localhost:8799/x", "localhost"),
            ("http://192.168.1.1/admin", "内网 IP"),
            ("http://169.254.169.254/latest/meta-data", "云元数据地址"),
            ("http://evil.example.com/a.png", "陌生域名"),
            ("file:///C:/Windows/win.ini", "file 协议"),
            ("http://i0.hdslb.com.evil.com/a.png", "后缀伪造域名"),
        ):
            code, body = _req(
                "GET", "/api/proxy_image?url=" + urllib.parse.quote(url, safe=""), TOK_ADMIN)
            ok(f"{label} -> 400", code == 400, f"{code} {body[:60]}")

        for url, label in (
            ("https://i0.hdslb.com/bfs/garb/item/xxx.png", "礼物图标域名"),
            ("https://i1.hdslb.com/bfs/face/xxx.jpg", "头像域名"),
            ("https://static.bilibili.com/a.png", "主域名"),
        ):
            reason = srv._check_proxy_image_url(url)
            ok(f"{label} 通过校验", reason == "", reason)

        # ── 4. 结构断言 ──
        print("[6] 结构断言")
        src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        ok("中间件不再只判断 role == \"user\"",
           '\n    if role == "user":' not in src)
        ok("非管理员统一走最小权限分支", 'if role != "admin":' in src)
        ok("存在全局密钥接口黑名单", "_SECRET_PATHS" in src)
        ok("密钥路径包含四个接口",
           all(p in src.split("_SECRET_PATHS: frozenset[str] = frozenset({")[1].split("})")[0]
               for p in ("/api/general_config", "/api/llm_config",
                         "/api/llm_config/export", "/api/templates")))
        ok("proxy_image 不再免登录",
           'or path.startswith("/api/proxy_image")' not in src)
        ok("proxy_image 有域名白名单", "_ALLOWED_IMAGE_SUFFIXES" in src)
        ok("proxy_image 边读边限长", "iter_chunked" in src)
        ok("proxy_image 读缓存走线程", "asyncio.to_thread(_load_image_cache" in src)
        ok("等待者带超时（不会永久挂住）",
           "asyncio.wait_for(asyncio.shield(waiter)" in src)
        ok("退出时一定唤醒等待者", "if not future.done():" in src)
        ok("礼物缓存失败后会重试", "_GIFT_CACHE_RETRY_AT" in src)
        gift_fail = src.split('logger.warning("get_gift_config timed out')[1].split(
            "cache_id: dict[int, str] = {}")[0]
        ok("礼物缓存失败不再永久置位", "_GIFT_CACHE_BUILT = True" not in gift_fail)
        ok("失败的两条路径都会排重试", gift_fail.count("_GIFT_CACHE_RETRY_AT = time.time()") == 2)
        # 前端建的账号角色确实是 streamer —— 这是本轮能利用的前提
        ok("前端新增主播账号写的是 role=streamer", "role: 'streamer'" in src)
        ok("旧格式迁移仍会把 streamer 归一为 user", 'role = "user"' in src)
        ok("房间配置响应经过脱敏", "_redact_llm_key(config_to_dict(cfg), request)" in src)
        ok("两个房间配置读接口都脱敏",
           src.count("_redact_llm_key(config_to_dict(cfg), request)") == 2)
    finally:
        server.should_exit = True
        time.sleep(0.4)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("ROLE / SECRETS / PROXY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
