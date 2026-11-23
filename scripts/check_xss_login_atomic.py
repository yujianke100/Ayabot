"""第二十一轮自检：日志面板 XSS、登录无限制、剩余的非原子写入。

本轮背景（都属于「同一个修法没铺开」或「漏了一整类输入」）：

1. bot.log 面板用 `v-html` 直出日志，而日志里有**观众可控**的文本
   （`danmaku received: text=%s`、`guard welcome: uname=%s`）。
   观众把昵称改成 `<img src=x onerror=...>` 或刷个粉丝牌，
   管理员一打开日志面板就在自己的会话里执行对方的脚本。

2. `/api/login` 在中间件里**先于** `_rate_limited` 返回，是唯一一个既免鉴权
   又完全没有限速的写接口 → 可以无限次爆破密码/用户名；而且请求体不限长度。

3. 第十一轮修了 config.yaml 的原子写，但另外 5 条**全局** config.yaml 写入
   路径（LLM 配置保存/导入、外部 API 密钥生成/清空/改域名）仍然是裸
   `write_text`；users.json 也是 —— 它被截断时 `_load_users` 返回 {}，
   接着 `_init_users` 会把默认弱口令管理员 `ayabot/123456` 写回去。

用法: ./.venv/Scripts/python.exe scripts/check_xss_login_atomic.py
"""
from __future__ import annotations

import json
import re
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

PORT = 19893
ROOM_A = "1900000555"
SRC = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")

XSS_PAYLOAD = '<img src=x onerror="window.__pwned=1">'


def _req(method: str, path: str, token: str | None = None,
         body: bytes | None = None) -> tuple[int, str]:
    headers = {"Cookie": f"session={token}"} if token else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    import uvicorn
    import yaml

    from app.web import server as srv

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_xss_"))
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    room_dir = tmp / "rooms" / ROOM_A
    (room_dir / "data").mkdir(parents=True, exist_ok=True)
    (room_dir / "config.yaml").write_text(
        yaml.safe_dump(example, allow_unicode=True, sort_keys=False), encoding="utf-8")
    # 模拟一条被观众注入过的日志
    (room_dir / "bot.log").write_text(
        "2026-01-01 10:00:00 INFO danmaku received: uid=1 text=%s\n" % XSS_PAYLOAD
        + "2026-01-01 10:00:01 INFO guard welcome: uid=2 uname=%s guard_level=3\n" % XSS_PAYLOAD,
        encoding="utf-8")

    srv._ROOMS_BASE_DIR = str(tmp)
    srv._ROOM_DB_PATH_CACHE.clear()
    srv._rate_limited = lambda ip: False  # type: ignore[assignment]
    _real_login_limited = srv._login_limited   # 第 5 段要恢复真实实现
    srv._login_limited = lambda ip, username="": False  # type: ignore[assignment]

    # ── 1. 结构断言：XSS ──
    print("[1] 日志/更新日志的 XSS 面")
    ok("日志面板仍有 v-html", 'v-html="botLogContent"' in SRC)
    ok("渲染前调用了 escapeHtml", "return escapeHtml(l)" in SRC)
    ok("存在 escapeHtml 实现", "function escapeHtml(s)" in SRC)
    ok("更新日志先转义再格式化", "return escapeHtml(text)" in SRC)
    ok("链接只放行 http(s)/mailto",
       "/^(https?:|mailto:)/i.test(href)" in SRC)
    ok("脚本注入不再有裸 v-html 输出日志",
       'return l\n                        .replace(/ERROR/g' not in SRC)

    # ── 2. 结构断言：登录限速 ──
    print("[2] 登录限速与请求体上限")
    ok("存在登录专用限速器", "def _login_limited(" in SRC)
    ok("IP 与用户名两个维度", "_LOGIN_MAX_IP_FAILS" in SRC and "_LOGIN_MAX_USER_FAILS" in SRC)
    ok("登录路径在中间件里先查限速",
       SRC.index('if path == "/api/login":')
       < SRC.index("if _rate_limited(client_ip):"))
    ok("登录请求体有上限", "_LOGIN_MAX_BODY" in SRC)
    ok("全局请求体有上限", "_MAX_REQUEST_BYTES" in SRC)
    ok("失败计数会被清理", '"login_fails": 0' in SRC)
    ok("登录响应不再回传 token", '{"ok": True, "token": token' not in SRC)

    # ── 3. 结构断言：原子写 ──
    print("[3] 剩余的非原子写入")
    ok("config.yaml 不再裸 write_text", "cfg_path.write_text" not in SRC)
    ok("users.json 使用原子写", "atomic_write_text(p, json.dumps(clean" in SRC)
    ok("templates.json 使用原子写", "atomic_write_text(p, json.dumps(data" in SRC)
    ok("gift_prices.json 使用原子写", "atomic_write_text(p, json.dumps(" in SRC)
    ok("crash_info 使用原子写", "atomic_write_text(path, json.dumps(payload" in SRC)
    ok("desired_states 使用原子写",
       "atomic_write_text(p, json.dumps(states" in SRC)
    ok("没有残留的裸 json.dumps 写文件",
       not re.search(r"\.write_text\(json\.dumps", SRC))

    # ── 4. 损坏的 users.json 不会重建默认弱口令账号 ──
    print("[4] users.json 损坏时不重建默认账号")
    auth_dir = tmp / "data"
    auth_dir.mkdir(parents=True, exist_ok=True)
    users_path = tmp / srv._AUTH_CONFIG_PATH
    users_path.parent.mkdir(parents=True, exist_ok=True)
    users_path.write_text('{"root": {"password_hash": "abc"', encoding="utf-8")  # 截断
    srv._USERS_CORRUPT = False
    loaded = srv._load_users()
    ok("损坏文件返回空表", loaded == {}, loaded)
    ok("标记为损坏", srv._USERS_CORRUPT is True)
    ok("留下了 .corrupt- 备份",
       any("corrupt" in p.name for p in users_path.parent.iterdir()), 
       [p.name for p in users_path.parent.iterdir()])
    srv._init_users()
    after = srv._load_users()
    ok("没有重建 ayabot 默认账号", "ayabot" not in after, list(after))
    srv._USERS_CORRUPT = False

    # ── 5. 端到端：登录限速 + 体积上限 ──
    print("[5] 端到端（真起一个 uvicorn）")
    srv._rate_limited = lambda ip: False  # type: ignore[assignment]
    srv._login_limited = _real_login_limited  # type: ignore[assignment]
    srv._LOGIN_FAILS.clear()
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
        # 超大 body
        big = json.dumps({"username": "x" * 5000, "password": "y"}).encode()
        code, body = _req("POST", "/api/login", body=big)
        ok("超长登录请求 -> 413", code == 413, f"{code} {body[:50]}")

        # 失败到限速
        code_last, msg_last = 0, ""
        for i in range(srv._LOGIN_MAX_USER_FAILS + 2):
            code_last, msg_last = _req(
                "POST", "/api/login",
                body=json.dumps({"username": "victim", "password": f"bad{i}"}).encode())
        ok("连续失败后被限速 -> 429", code_last == 429, f"{code_last} {msg_last[:60]}")
        # 即使密码正确也被挡住（说明限速在比对之前生效）
        code, body = _req("POST", "/api/login",
                          body=json.dumps({"username": "victim", "password": "whatever"}).encode())
        ok("限速期内正确密码同样 429", code == 429, f"{code} {body[:60]}")
        # 换一个用户名不受影响（用户名维度独立）
        code, body = _req("POST", "/api/login",
                          body=json.dumps({"username": "other", "password": "bad"}).encode())
        ok("换用户名不被上一个用户连累", code == 403, f"{code} {body[:60]}")
        body_json = json.loads(body)
        ok("失败响应里没有 token", "token" not in body_json, body_json)

        # XML 导入必须放行到 64MB（不能被 8MB 的全局上限掐掉）
        ok("XML 导入路径使用 64MB 上限",
           'path.endswith("/danmaku/import")' in SRC
           and "_MAX_XML_BYTES if" in SRC)
        ok("导入上限来自导入模块本身",
           "from app.danmaku_xml import MAX_XML_BYTES as _MAX_XML_BYTES" in SRC)
    finally:
        server.should_exit = True
        time.sleep(0.4)

    # ── 6. 原子写不留临时文件 ──
    print("[6] 原子写的临时文件不会残留")
    target = tmp / "atomic_probe.json"
    srv.atomic_write_text(target, '{"a":1}')
    ok("内容写入正确", json.loads(target.read_text(encoding="utf-8")) == {"a": 1})
    ok("没有残留 .tmp 文件", not list(tmp.glob(".atomic_probe.json.tmp*")),
       [p.name for p in tmp.glob("*tmp*")])

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("XSS / LOGIN / ATOMIC OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
