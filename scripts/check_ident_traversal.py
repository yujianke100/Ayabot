"""第二十二轮自检：路径穿越（房间号 / 账号 uid 拼进文件路径）。

本轮背景：房间号和 uid 来自 URL，并且被直接拼进文件路径
（`rooms/<id>/`、`accounts/<uid>/`）。中间件里用来匹配房间路径的正则是
`^/api/rooms/([^/]+)(/.*)?$` —— `[^/]+` **放行反斜杠**，而 URL 里的 `%5C`
会被解码成 `\\`，于是 `..\\xxx` 能跳出 rooms/ 目录。

修复前实测（真起 uvicorn，管理员会话）：

  DELETE /api/rooms/..%5Cvictim            -> 200 {"ok":true}  目录被整个删除
  POST   /api/bili_accounts/..%5Cacc/nickname -> 200           任意目录写入 meta.yaml
  DELETE /api/bili_accounts/..%5Cacc       -> 200              目录被删除
  GET    /api/bili_accounts（普通账号）    -> 200              列出全站 B站账号

修复：白名单校验标识符形状（`_IDENT_RE`）+ `_safe_subdir()` 兜底
（resolve 后必须真的在 base 里）+ B站账号接口整体只给管理员。

顺带把 `credential.json` 的写入改成原子写 —— 这个文件被截断就是
「登录凭证失效」，和用户反馈的第一个症状同源。

用法: ./.venv/Scripts/python.exe scripts/check_ident_traversal.py
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

PORT = 19891
ROOM = "1900000888"
ACC = "3546775331801924"
TOK_ADMIN = "tok-admin-22"
TOK_USER = "tok-user-22"


def _req(method: str, path: str, token: str = TOK_ADMIN,
         body: bytes | None = None) -> tuple[int, str]:
    headers = {"Cookie": f"session={token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
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

    # ── 1. 纯函数：标识符校验 ──
    print("[1] 标识符白名单")
    for good in ("1946287911", "2000000002", "abc-DEF_1", "1"):
        ok(f"{good!r} 合法", srv._valid_ident(good))
    for bad in ("..", "../x", "..\\x", "a/b", "a\\b", "", "x" * 33,
                "a b", "a%5Cb", ".", "a.", "CON:*"):
        ok(f"{bad!r} 非法", not srv._valid_ident(bad))

    print("[2] _safe_subdir 只在 base 内部返回路径")
    tmp_probe = Path(tempfile.mkdtemp(prefix="ayabot_safe_"))
    (tmp_probe / "ok_dir").mkdir()
    (tmp_probe.parent / "outside_probe").mkdir(exist_ok=True)
    ok("正常子目录可用", srv._safe_subdir(tmp_probe, "ok_dir") is not None)
    for bad in ("..", "../outside_probe", "..\\outside_probe", "C:\\Windows",
                "/etc", ""):
        ok(f"{bad!r} 被拒绝", srv._safe_subdir(tmp_probe, bad) is None)
    shutil.rmtree(tmp_probe, ignore_errors=True)

    # ── 3. 端到端 ──
    tmp = Path(tempfile.mkdtemp(prefix="ayabot_trav_"))
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    room_dir = tmp / "rooms" / ROOM
    (room_dir / "data").mkdir(parents=True, exist_ok=True)
    (room_dir / "config.yaml").write_text(
        yaml.safe_dump(example, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # 攻击目标：tmp/victim（room_id = "..\victim" → tmp/rooms/../victim）
    victim = tmp / "victim"
    victim.mkdir()
    (victim / "IMPORTANT.txt").write_text("do not delete me", encoding="utf-8")
    # 账号攻击目标：tmp/accvictim
    accvictim = tmp / "accvictim"
    accvictim.mkdir()
    (accvictim / "KEEP.txt").write_text("keep", encoding="utf-8")
    # 一个真实账号（用来验证合法 uid 仍然可用 + 列表接口）
    real_acc = tmp / "accounts" / ACC
    real_acc.mkdir(parents=True)
    (real_acc / "credential.json").write_text("{}", encoding="utf-8")
    (real_acc / "meta.yaml").write_text("nickname: real\n", encoding="utf-8")

    srv._ROOMS_BASE_DIR = str(tmp)
    srv._ROOM_DB_PATH_CACHE.clear()
    srv._rate_limited = lambda ip: False  # type: ignore[assignment]
    srv._login_limited = lambda ip, username="": False  # type: ignore[assignment]
    exp = time.time() + 900
    srv._SESSIONS[TOK_ADMIN] = (exp, "root", "admin", [])
    srv._SESSIONS[TOK_USER] = (exp, "anchor", "user", [ROOM])

    async def _fake_ok(rid):  # noqa: ANN001
        return True

    srv.start_room_async = _fake_ok        # type: ignore[assignment]
    srv.stop_room_async = _fake_ok         # type: ignore[assignment]
    srv.restart_room_async = _fake_ok      # type: ignore[assignment]
    srv.room_status = lambda rid: "stopped"  # type: ignore[assignment]
    srv.clean_room = lambda rid: None      # type: ignore[assignment]

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
        print("[3] 房间号穿越：不能删/读目录外面的东西")
        for pid, label in (("..%5Cvictim", "反斜杠 + 上级目录"),
                           ("..%5C..%5Cvictim", "两级上跳"),
                           ("%2E%2E%5Cvictim", "编码过的点"),
                           ("C%3A%5CWindows", "绝对路径")):
            code, body = _req("DELETE", f"/api/rooms/{pid}")
            ok(f"删除房间 {label} -> 400", code == 400, f"{code} {body[:50]}")
        ok("攻击目标目录仍然存在", victim.exists() and (victim / "IMPORTANT.txt").exists())

        for pid in ("..%5Cvictim", "..%5C..%5Cvictim"):
            code, body = _req("GET", f"/api/rooms/{pid}/config")
            ok(f"读配置穿越 {pid} -> 400", code == 400, f"{code} {body[:50]}")
            code, body = _req("GET", f"/api/rooms/{pid}/log/export")
            ok(f"导出日志穿越 {pid} -> 400", code == 400, f"{code} {body[:50]}")

        print("[4] 账号 uid 穿越：不能写/删目录外面的东西")
        code, body = _req("POST", "/api/bili_accounts/..%5Caccvictim/nickname",
                          body=json.dumps({"nickname": "pwned"}).encode())
        ok("写昵称穿越 -> 400", code == 400, f"{code} {body[:50]}")
        ok("没有在目标目录写出 meta.yaml", not (accvictim / "meta.yaml").exists())
        code, body = _req("DELETE", "/api/bili_accounts/..%5Caccvictim")
        ok("删账号穿越 -> 400", code == 400, f"{code} {body[:50]}")
        ok("账号攻击目标目录仍然存在", accvictim.exists() and (accvictim / "KEEP.txt").exists())

        print("[5] 合法标识符仍然可用（没有把功能一起锁死）")
        code, body = _req("GET", f"/api/rooms/{ROOM}/config")
        ok("读自己房间配置 -> 200", code == 200, f"{code}")
        code, body = _req("POST", f"/api/rooms/{ROOM}/config",
                          body=json.dumps({"bot_name": "x"}).encode())
        ok("写自己房间配置 -> 200", code == 200, f"{code}")
        code, body = _req("POST", f"/api/bili_accounts/{ACC}/nickname",
                          body=json.dumps({"nickname": "新名字"}).encode())
        ok("改真实账号昵称 -> 200", code == 200, f"{code} {body[:50]}")
        ok("昵称确实写入", "新名字" in (real_acc / "meta.yaml").read_text(encoding="utf-8"))

        print("[6] 新建房间也要校验房间号")
        code, body = _req("POST", "/api/rooms",
                          body=json.dumps({"anchor_uid": 1,
                                           "room_display_id": "..\\evil"}).encode())
        ok("新建房间用穿越号 -> 400", code == 400, f"{code} {body[:60]}")
        ok("没有在上级目录建出房间",
           not (tmp / "evil").exists() and not (tmp.parent / "evil").exists())

        print("[7] B站账号列表只给管理员")
        code, body = _req("GET", "/api/bili_accounts", token=TOK_USER)
        ok("普通账号读账号列表 -> 403", code == 403, f"{code} {body[:60]}")
        ok("响应里没有账号 uid", ACC not in body)
        code, body = _req("GET", "/api/bili_accounts", token=TOK_ADMIN)
        ok("管理员读账号列表 -> 200", code == 200 and ACC in body, f"{code}")
        for sub in ("status?session_id=x", f"{ACC}/verify"):
            code, body = _req("GET", f"/api/bili_accounts/{sub}", token=TOK_USER)
            ok(f"普通账号 {sub} -> 403", code == 403, f"{code} {body[:40]}")

        print("[8] 删除真实房间仍然工作（rmtree 只在 rooms/ 内部）")
        code, body = _req("DELETE", f"/api/rooms/{ROOM}")
        ok("删除自己的房间 -> 200", code == 200, f"{code} {body[:50]}")
        ok("房间目录确实被删", not room_dir.exists())
        ok("上级目录没被连坐", victim.exists() and (tmp / "rooms").exists())

        # ── 9. 结构断言 ──
        print("[9] 结构断言")
        src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        auth_src = (ROOT / "app" / "auth.py").read_text(encoding="utf-8")
        writes = re.findall(r"\.write_text\(", src)
        ok("server.py 只剩「清空日志」一处裸 write_text", len(writes) == 1, writes)
        ok("账号目录走 _safe_subdir", "_safe_subdir(base, str(uid))" in src)
        ok("非法 uid 会抛而不是拼路径", 'raise ValueError(f"非法账号 uid' in src)
        ok("删除房间再校验一次", "_safe_subdir(rooms_root, str(room_id))" in src)
        ok("中间件校验房间号", "_valid_ident(_m_room.group(1))" in src)
        ok("中间件校验账号 uid", "_valid_ident(_m_acc.group(1))" in src)
        ok("校验在 admin 短路之前",
           src.index("_valid_ident(_m_room.group(1))") < src.index('if role == "admin":\n        return await call_next'))
        ok("B站账号前缀只给管理员", '"/api/bili_accounts",' in src
           and "_ADMIN_ONLY_PREFIXES" in src)
        ok("凭证写入是原子写（auth.py）",
           "atomic_write_text(path, json.dumps(payload" in auth_src)
        # 凭据有三条写入路径：老扫码保存（accounts/<uid>/ + 旧路径兜底）、
        # 账号页扫码保存、以及 auth.py 的刷新落盘。全部必须原子写，
        # 且不允许再出现 `credential.json").write_text(` 这种裸写。
        ok("凭证写入全部原子写",
           src.count('atomic_write_text(acc_dir / "credential.json"') >= 2
           and "atomic_write_text(legacy_path" in src
           and 'credential.json").write_text(' not in src
           and "atomic_write_text(path, json.dumps(payload" in auth_src)
        ok("新建房间校验房间号", 'if not _valid_ident(room_id):' in src)
    finally:
        server.should_exit = True
        time.sleep(0.4)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("IDENT / TRAVERSAL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
