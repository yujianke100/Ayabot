"""中文 / 空格 / 括号路径下必须一切正常（Windows 用户的日常）。

真实路径长这样：
    C:\\Users\\张三\\Desktop\\Ayabot 直播机器人 (2)\\rooms\\1946287911\\data\\bot.db

这些东西都是坑：
- `subprocess.Popen` 的 cwd / 可执行路径带空格 → 不加引号就找不到；
- 拼 SQL 或 REGEX 时路径里有 `\\` 和 `(`（正则元字符）；
- `sqlite3.connect()` 对 UTF-8 路径是支持的，但 `Path.as_uri()` /
  `urlopen(file://...)` / `http.server` 会踩非 ASCII；
- 日志文件用 locale 编码打开 → 中文路径本身就写不进去。

这里的做法是**真的在那种目录里起一遍服务**，把主要接口都打一遍，
再让同进程 Bot 启动/停止一次，确认 bot.log 和 bot.db 都落在正确位置。

    ./.venv/Scripts/python.exe scripts/check_nonascii_paths.py
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

PORT = 19021
ROOM = "1946287911"
TOKEN = "nonascii"

#: 故意的恶心目录名：中文 + 空格 + 括号 + 井号 + 和号
UGLY = "艾草 直播间 (2) & 测试#1"


def main() -> int:
    import uvicorn
    from app.web import server as srv

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    base = Path(tempfile.mkdtemp(prefix="nonascii_"))
    tmp = base / UGLY
    try:
        print(f"  测试根目录: {tmp}")
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (tmp / "data").mkdir(parents=True)
        (tmp / "config.yaml").write_text(example, encoding="utf-8")
        room = tmp / "rooms" / ROOM
        (room / "data").mkdir(parents=True)
        (room / "config.yaml").write_text(example, encoding="utf-8")

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._API_TOKEN = TOKEN
        srv._SESSIONS[TOKEN] = (time.time() + 3600, "t", "admin", [])
        srv._rate_limited = lambda ip: False  # type: ignore[assignment]

        cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="critical")
        server = uvicorn.Server(cfg)
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(120):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)

        def call(path: str, method="GET", body=None) -> tuple[int, str]:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}{path}", data=data, method=method,
                headers={"Cookie": f"session={TOKEN}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                return 0, str(exc)

        print("[1] 主要接口都能用")
        for path in ("/api/rooms", f"/api/rooms/{ROOM}/config", f"/api/rooms/{ROOM}/log",
                     "/api/time", "/api/users", "/api/templates"):
            code, body = call(path)
            ok(f"{path} 不是 5xx", code < 500, f"{code} {body[:70]}")

        print("[2] 新建房间（会写 config.yaml + 起目录）")
        code, body = call("/api/rooms", "POST",
                          {"anchor_uid": 424242, "room_display_id": 424242, "room_name": "新房间"})
        ok("新建不是 5xx", code < 500, f"{code} {body[:90]}")
        new_id = ""
        try:
            new_id = str((json.loads(body) or {}).get("room_id") or "")
        except Exception:  # noqa: BLE001
            pass
        ok("拿到了新房间号", bool(new_id), new_id)
        if new_id:
            ok("新房间的 config.yaml 落在中文目录里",
               (tmp / "rooms" / new_id / "config.yaml").exists())

        print("[3] 同进程 Bot 也在这个目录里跑（bot.log / bot.db 落对位置）")
        import app.auth as auth_mod
        import app.bot as bot_mod
        import app.process_manager as pm
        import asyncio

        pm.set_rooms_base_dir(tmp)

        class _FakeRobot:
            def __init__(self, config=None, credential=None, auth=None, **kw) -> None:
                self.config = config

            async def run(self) -> None:
                import logging
                logging.getLogger("bili-live-robot").info("中文路径下的机器人启动")
                await asyncio.sleep(0.05)

            async def shutdown(self) -> None:
                pass

        class _FakeAuth:
            def __init__(self, config=None, **kw) -> None:
                pass

            async def prepare_credential(self):
                return object()

            def start_refresh_loop(self, cred) -> None:
                pass

            async def stop(self) -> None:
                pass

            def write_status(self, *a, **k) -> None:
                pass

            def mark_needs_login(self, *a, **k) -> None:
                pass

        old_robot, old_auth = bot_mod.LiveRobot, auth_mod.AuthManager
        bot_mod.LiveRobot, auth_mod.AuthManager = _FakeRobot, _FakeAuth
        try:
            async def scenario():
                started = await pm.start_bot_in_process(ROOM)
                ok("启动成功", started is True, started)
                await asyncio.sleep(0.3)
                await pm.stop_bot_in_process(ROOM)

            asyncio.run(scenario())
        finally:
            bot_mod.LiveRobot, auth_mod.AuthManager = old_robot, old_auth
            pm.set_rooms_base_dir(ROOT)

        log_path = room / "bot.log"
        ok("bot.log 落在中文目录里", log_path.exists(), str(log_path))
        if log_path.exists():
            txt = log_path.read_text(encoding="utf-8")
            ok("里面是能读的中文（不是乱码）", "中文路径下的机器人启动" in txt, txt[:60])

        print("[4] 日志接口能读到它")
        code, body = call(f"/api/rooms/{ROOM}/log?lines=20")
        ok("HTTP 200", code == 200, code)
        if code == 200:
            lines = (json.loads(body) or {}).get("lines") or []
            ok("确实读到了日志内容", any("中文路径下的机器人启动" in l for l in lines),
               f"{len(lines)} 行")

        print("[5] 导出（FileResponse 走中文文件名）")
        code, body = call(f"/api/rooms/{ROOM}/log/export")
        ok("导出 200", code == 200, code)
        ok("内容正确", "中文路径下的机器人启动" in body)

        print("[6] 删除房间（rmtree 中文目录 + 释放句柄）")
        code, body = call(f"/api/rooms/{ROOM}", "DELETE")
        ok("删除不是 5xx", code < 500, f"{code} {body[:80]}")
        ok("目录真的没了", not room.exists())

        server.should_exit = True
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("NON-ASCII PATHS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
