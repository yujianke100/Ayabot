"""守护进程逻辑验证：进程崩溃自动拉起、心跳假死重启、崩溃限流。

不开真实直播间、不发任何请求，只对 server._supervise_once 做替身测试。

用法: ./.venv/Scripts/python.exe scripts/check_supervisor.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/supervisor_test")
ROOM = "999999999"


async def main() -> int:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    (SCRATCH / "rooms" / ROOM / "data").mkdir(parents=True, exist_ok=True)
    (SCRATCH / "rooms" / ROOM / "config.yaml").write_text("room_display_id: 1\nanchor_uid: 2\n", encoding="utf-8")

    import app.web.server as srv

    srv._ROOMS_BASE_DIR = str(SCRATCH)

    calls: list[str] = []
    state = {"status": "stopped"}

    srv._list_rooms_from_disk = lambda: [{"room_id": ROOM}]          # type: ignore[assignment]
    srv.room_status = lambda rid: state["status"]                     # type: ignore[assignment]

    async def fake_restart(rid: str) -> bool:
        calls.append(rid)
        state["status"] = "running"
        return True

    srv.restart_room_async = fake_restart                              # type: ignore[assignment]
    srv._restart_attempts.clear()
    srv._zombie_restarts.clear()

    failures: list[str] = []
    srv._save_desired_states({ROOM: True})

    # 1) 期望运行 + 进程已退出 → 自动重启
    await srv._supervise_once()
    print("after crash: calls =", calls, "status =", state["status"])
    if calls != [ROOM]:
        failures.append("进程退出后没有自动重启")
    crash = json.loads((SCRATCH / "rooms" / ROOM / "data" / "crash_info.json").read_text(encoding="utf-8"))
    print("crash_info:", crash)
    if crash.get("reason") != "进程已退出":
        failures.append(f"crash_info 原因不对: {crash}")

    # 2) 期望停止 → 不重启
    calls.clear()
    srv._save_desired_states({ROOM: False})
    state["status"] = "stopped"
    await srv._supervise_once()
    print("after manual stop: calls =", calls)
    if calls:
        failures.append("用户主动停止后仍被守护进程拉起")

    # 3) 进程在跑但心跳超时 → 判假死重启
    srv._save_desired_states({ROOM: True})
    state["status"] = "running"
    calls.clear()
    (SCRATCH / "rooms" / ROOM / "data" / "bot_status.json").write_text(
        json.dumps({"ts": int(time.time()) - 600}), encoding="utf-8"
    )
    await srv._supervise_once()
    print("after stale heartbeat: calls =", calls)
    if calls != [ROOM]:
        failures.append("心跳超时没有触发重启")
    # 冷却期内不重复重启
    calls.clear()
    await srv._supervise_once()
    print("stale again within cooldown: calls =", calls)
    if calls:
        failures.append("假死重启没有冷却，可能反复重启")

    # 4) 心跳新鲜 → 不动
    calls.clear()
    (SCRATCH / "rooms" / ROOM / "data" / "bot_status.json").write_text(
        json.dumps({"ts": int(time.time()) - 10}), encoding="utf-8"
    )
    srv._zombie_restarts.clear()
    await srv._supervise_once()
    print("fresh heartbeat: calls =", calls)
    if calls:
        failures.append("心跳新鲜时不该重启")

    # 5) 崩溃限流：15 分钟内最多 5 次
    srv._restart_attempts.clear()
    now = time.time()
    allowed = [srv._restart_allowed(ROOM, now + i) for i in range(7)]
    for _ in range(5):
        srv._restart_attempts.setdefault(ROOM, []).append(now)
    print("throttle allowed seq:", allowed, "after 5 attempts:", srv._restart_allowed(ROOM, now + 1))
    if srv._restart_allowed(ROOM, now + 1):
        failures.append("超过 5 次仍未限流")
    if not srv._restart_allowed(ROOM, now + srv._CRASH_COOLDOWN + 5):
        failures.append("冷却结束后应允许重启")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nSUPERVISOR OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
