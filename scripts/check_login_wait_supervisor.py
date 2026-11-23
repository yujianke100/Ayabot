"""第二十三轮自检：凭证失效时守护进程把「等扫码」当成僵尸任务无限重启。

本轮背景（这条直接对应两个用户反馈：「登录凭证失效时报错/不引导重新登录」
和「机器人莫名其妙关闭，重启就好」）：

`prepare_credential()` 在凭证失效且 `wait_for_login=True`（默认）时进入
`wait_for_relogin()` 阻塞等待扫码。此时 `LiveRobot.run()` 还没开始跑，
所以 `bot_status.json` **一个字节都没写过**。而守护进程的判断是：

    if room_status(room_id) != "running": 恢复
    ts = bot_status.json 的 ts
    if not ts: 等 120s → 判定「僵尸任务」→ 写崩溃横幅 → 重启

于是「等你扫码」被判定成「僵尸任务」：每 10 分钟（冷却）重启一次进程，
每次都写一条 `运行中但 XXXs 没有心跳（疑似僵尸任务）` 的红色横幅。
重启后仍是同样状态，永远好不了 —— 用户看到「机器人莫名其妙关了、又自己起来」，
点一下重启「就好了」（其实只是又重启了一次，真正恢复靠的是他后来扫码）。

修复（两层）：
1. `AuthManager` 在等待扫码期间每 5 秒写一次心跳（phase=waiting_login），
   让「有没有心跳」这个信号恢复真实。
2. 守护进程额外识别「当前运行的进程正处于 needs_login」（pid 对得上就跳过），
   心跳写失败时也不会再重启。

用法: ./.venv/Scripts/python.exe scripts/check_login_wait_supervisor.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROOM = "1900000999"
SRC = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
AUTH_SRC = (ROOT / "app" / "auth.py").read_text(encoding="utf-8")
BOT_SRC = (ROOT / "app" / "bot.py").read_text(encoding="utf-8")


def main() -> int:
    from app.config import HEARTBEAT_FILE
    from app.web import server as srv

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_waitlogin_"))
    room_dir = tmp / "rooms" / ROOM
    data = room_dir / "data"
    data.mkdir(parents=True)
    (room_dir / "config.yaml").write_text("room_display_id: 1\n", encoding="utf-8")
    (tmp / "data").mkdir(exist_ok=True)
    (tmp / "data" / "desired_states.json").write_text(
        json.dumps({ROOM: True}), encoding="utf-8")

    srv._ROOMS_BASE_DIR = str(tmp)
    srv.set_rooms_base_dir(str(tmp))          # process_manager 也要指到同一个根
    srv._ROOM_DB_PATH_CACHE.clear()
    srv.room_status = lambda rid: "running"    # type: ignore[assignment]
    restarts: list[str] = []

    async def _fake_restart(rid):  # noqa: ANN001
        restarts.append(rid)
        return True

    srv.restart_room_async = _fake_restart     # type: ignore[assignment]

    def reset_sup_state() -> None:
        srv._no_heartbeat_since.clear()
        srv._zombie_restarts.clear()

    def write_auth_status(payload: dict) -> None:
        (data / "auth_status.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def write_pidfile(pid: int) -> None:
        (room_dir / "bot.pid").write_text(str(pid), encoding="utf-8")

    try:
        # ── 1. 等扫码 + pid 对得上 → 不重启、不写崩溃横幅 ──
        print("[1] 凭证失效等待扫码：不能当成僵尸任务重启")
        write_pidfile(os.getpid())
        write_auth_status({
            "status": "needs_login",
            "reason": "B站登录凭证已失效（Cookie 过期或被踢下线）",
            "account_uid": "3546775331801924", "room_id": ROOM,
            "ts": int(time.time()), "pid": os.getpid(),
        })
        (data / "crash_info.json").unlink(missing_ok=True)
        reset_sup_state()
        srv._no_heartbeat_since[ROOM] = time.time() - 1000   # 早已过宽限
        restarts.clear()
        asyncio.run(srv._supervise_once())
        ok("等待扫码时未重启", restarts == [], restarts)
        ok("没有写崩溃横幅", not (data / "crash_info.json").exists())
        ok("也清理了 no-heartbeat 计时", ROOM not in srv._no_heartbeat_since)

        # ── 2. 心跳超时路径同样要跳过（不是只有「从未写过心跳」这条）──
        print("[2] 有心跳但超时，也要先看是不是在等扫码")
        (data / "bot_status.json").write_text(
            json.dumps({"ts": int(time.time()) - 1000, "phase": "running"}),
            encoding="utf-8")
        reset_sup_state()
        restarts.clear()
        (data / "crash_info.json").unlink(missing_ok=True)
        asyncio.run(srv._supervise_once())
        ok("心跳超时但等扫码 → 未重启", restarts == [], restarts)
        ok("未写崩溃横幅", not (data / "crash_info.json").exists())

        # ── 3. 进程已经死了（pid 对不上）→ 必须重启（扫码后要能自动恢复）──
        print("[3] 状态文件是上一次运行留下的：进程已死就必须重启")
        write_pidfile(999999)                 # 一个不存在的 pid
        write_auth_status({
            "status": "needs_login", "reason": "x", "account_uid": "",
            "room_id": ROOM, "ts": int(time.time()) - 3600, "pid": 424242,
        })
        reset_sup_state()
        srv._no_heartbeat_since[ROOM] = time.time() - 1000
        restarts.clear()
        asyncio.run(srv._supervise_once())
        ok("pid 不匹配时不跳过", restarts == [ROOM], restarts)

        # ── 4. 真正的僵尸任务仍要被发现（别把功能修没了）──
        print("[4] 正常的僵尸检测不能被破坏")
        write_pidfile(os.getpid())
        write_auth_status({"status": "ok", "reason": "", "room_id": ROOM,
                           "ts": int(time.time()), "pid": os.getpid()})
        reset_sup_state()
        srv._no_heartbeat_since[ROOM] = time.time() - 1000
        restarts.clear()
        asyncio.run(srv._supervise_once())
        ok("auth ok + 无心跳 → 仍然重启", restarts == [ROOM], restarts)
        ok("仍然写了崩溃横幅", (data / "crash_info.json").exists())

        # ── 5. AuthManager 在等待期间写心跳 ──
        print("[5] 等待扫码期间每轮都写心跳（phase=waiting_login）")
        import yaml
        from app.auth import AuthManager
        from app.config import load_config

        cfg_file = room_dir / "config.yaml"
        raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        raw["room_display_id"] = int(ROOM)
        raw["storage"] = {"sqlite_path": "data/bot.db"}
        raw["auth"] = {"wait_for_login": True, "credential_store_path": "data/credential.json"}
        cfg_file.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        cfg = load_config(str(cfg_file))
        auth = AuthManager(cfg)
        auth._reason = "B站登录凭证已失效（Cookie 过期或被踢下线）"
        auth._write_waiting_heartbeat()
        hb = json.loads((data / HEARTBEAT_FILE).read_text(encoding="utf-8"))
        ok("心跳 phase=waiting_login", hb.get("phase") == "waiting_login", hb.get("phase"))
        ok("心跳 ts 是当前时间", abs(int(hb.get("ts", 0)) - int(time.time())) <= 5)
        ok("心跳 pid 是本进程", int(hb.get("pid") or 0) == os.getpid())
        ok("标记了 waiting_login", hb.get("waiting_login") is True)
        ok("danmaku_connected=False", hb.get("danmaku_connected") is False)

        # 心跳文件时间戳要前进（证明 wait_for_relogin 每轮都刷新）
        first_ts = int(hb.get("ts") or 0)
        time.sleep(1.2)
        import app.auth as auth_mod

        old_poll = auth_mod._LOGIN_WAIT_POLL
        auth_mod._LOGIN_WAIT_POLL = 0.2
        try:
            async def _poll_twice() -> None:
                task = asyncio.create_task(auth.wait_for_relogin())
                await asyncio.sleep(0.5)
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

            asyncio.run(_poll_twice())
        finally:
            auth_mod._LOGIN_WAIT_POLL = old_poll
        hb2 = json.loads((data / HEARTBEAT_FILE).read_text(encoding="utf-8"))
        ok("等待期间心跳持续刷新", int(hb2.get("ts") or 0) > first_ts,
           f"{first_ts} -> {hb2.get('ts')}")

        # ── 6. 结构断言 ──
        print("[6] 结构断言")
        ok("心跳文件名是共享常量", HEARTBEAT_FILE == "bot_status.json")
        ok("bot.py 不再自己定义", 'HEARTBEAT_FILE = "bot_status.json"' not in BOT_SRC)
        ok("bot.py 从 config 导入", "from .config import AppConfig, HEARTBEAT_FILE" in BOT_SRC)
        ok("auth.py 从 config 导入", "HEARTBEAT_FILE" in AUTH_SRC.split("from .config import")[1][:200])
        ok("存在等待心跳写入函数", "def _write_waiting_heartbeat" in AUTH_SRC)
        # 「进入等待前先写一次」：prepare_credential 里那句必须在 return 之前
        prep = AUTH_SRC.split("async def wait_for_relogin")[0]
        ok("进入等待前也写一次（缩短空窗）",
           "self._write_waiting_heartbeat()" in prep
           and "return await self.wait_for_relogin()" in prep)
        ok("两处调用都在", AUTH_SRC.count("self._write_waiting_heartbeat()") == 2,
           AUTH_SRC.count("self._write_waiting_heartbeat()"))
        ok("循环里先写心跳再 sleep",
           "_write_waiting_heartbeat()\n            await asyncio.sleep(_LOGIN_WAIT_POLL)"
           in AUTH_SRC)
        ok("守护进程有等扫码判定", "def _room_waiting_login" in SRC)
        ok("判定要求 pid 对上", "int(running_pid) == status_pid" in SRC)
        ok("无心跳路径先判等扫码",
           SRC.index("if _room_waiting_login(room_id):")
           < SRC.index("if not ts:"))
        ok("needs_login 不是 ok 就够了（还看 pid）",
           '_read_pid' in SRC and "_inproc_is_running" in SRC)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("LOGIN WAIT / SUPERVISOR OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
