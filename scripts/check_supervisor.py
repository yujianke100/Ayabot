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

    # 6) 限流触发时必须留下可展示的崩溃记录（否则房间静默死掉，用户不知道原因）
    print("[6] 暂停自动重启要写 crash_info")
    srv._restart_attempts.clear()
    calls.clear()
    data_dir = SCRATCH / "rooms" / ROOM / "data"
    (data_dir / "crash_info.json").unlink(missing_ok=True)
    now = time.time()
    for i in range(srv._CRASH_MAX_ATTEMPTS):
        srv._restart_attempts.setdefault(ROOM, []).append(now - 10 + i)
    srv._save_desired_states({ROOM: True})
    state["status"] = "stopped"
    await srv._supervise_once()
    paused_info = json.loads((data_dir / "crash_info.json").read_text(encoding="utf-8"))
    print("paused crash_info:", paused_info)
    if calls:
        failures.append("限流期间不该再重启")
    if not paused_info.get("paused"):
        failures.append("限流时 crash_info 没有 paused 标记")
    if "暂停自动重启" not in str(paused_info.get("reason") or ""):
        failures.append(f"限流时 crash_info 原因不清晰: {paused_info.get('reason')}")
    if not paused_info.get("retry_at"):
        failures.append("限流时 crash_info 没有 retry_at")

    # 7) Bot 自己写的崩溃详情要合并进原因
    print("[7] bot_error.json 合并")
    srv._restart_attempts.clear()
    calls.clear()
    (data_dir / "bot_error.json").write_text(json.dumps(
        {"ts": int(time.time()), "reason": "ValueError: 坏掉的 payload | a / b / c"}, ensure_ascii=False),
        encoding="utf-8")
    state["status"] = "stopped"
    await srv._supervise_once()
    merged = json.loads((data_dir / "crash_info.json").read_text(encoding="utf-8"))
    print("merged crash_info:", merged)
    if "坏掉的 payload" not in str(merged.get("reason") or ""):
        failures.append(f"崩溃详情没合并进 crash_info: {merged.get('reason')}")
    if (data_dir / "bot_error.json").exists():
        failures.append("bot_error.json 读后没有删除，下一次崩溃会用到旧错误")
    if merged.get("paused"):
        failures.append("正常重启不该标 paused")

    # 8) 手动重启要清掉崩溃状态与限流计数
    print("[8] 手动重启清理崩溃状态")
    srv._restart_attempts.setdefault(ROOM, []).extend([now] * 5)
    srv._zombie_restarts[ROOM] = now
    srv._no_heartbeat_since[ROOM] = now
    (data_dir / "crash_info.json").write_text('{"paused": true}', encoding="utf-8")
    (data_dir / "bot_error.json").write_text('{"reason": "旧错误"}', encoding="utf-8")
    srv._clear_crash_state(ROOM)
    if srv._restart_attempts.get(ROOM) or srv._zombie_restarts.get(ROOM):
        failures.append("_clear_crash_state 没有清掉限流计数")
    if (data_dir / "crash_info.json").exists() or (data_dir / "bot_error.json").exists():
        failures.append("_clear_crash_state 没有删掉崩溃记录文件")

    # 9) _parse_date 必须按配置时区算（不能是系统本地时区）
    print("[9] _parse_date 时区")
    from app import tzutil
    old_tz = tzutil.get_tz()
    tzutil.set_config_timezone("UTC")
    lo_utc, hi_utc = srv._parse_date("2024-12-31")
    tzutil.set_config_timezone("Asia/Shanghai")
    lo_sh, hi_sh = srv._parse_date("2024-12-31")
    tzutil.set_config_timezone(str(old_tz))
    print(f"  UTC [{lo_utc},{hi_utc})  +08 [{lo_sh},{hi_sh})")
    if lo_utc != 1735603200:      # 2024-12-31T00:00Z
        failures.append(f"_parse_date(UTC) 起点不对: {lo_utc}")
    if lo_sh != lo_utc - 8 * 3600:
        failures.append(f"_parse_date(+08) 与 UTC 不是差 8 小时: {lo_sh} vs {lo_utc}")
    if hi_utc - lo_utc != 86400:
        failures.append(f"_parse_date 区间不是一天: {hi_utc - lo_utc}")

    # 10) Bot 崩溃详情写入（process_manager._write_bot_error）
    print("[10] bot_error 写入")
    from app.process_manager import _write_bot_error
    err_dir = SCRATCH / "boterr"
    try:
        raise RuntimeError("模拟的送礼处理崩溃")
    except RuntimeError as exc:
        _write_bot_error(err_dir, exc)
    err_file = err_dir / "bot_error.json"
    if not err_file.exists():
        failures.append("_write_bot_error 没有写出文件")
    else:
        err = json.loads(err_file.read_text(encoding="utf-8"))
        print("  bot_error:", err)
        if "RuntimeError" not in str(err.get("reason")):
            failures.append(f"bot_error 缺异常类型: {err.get('reason')}")
        if "模拟的送礼处理崩溃" not in str(err.get("reason")):
            failures.append(f"bot_error 缺异常消息: {err.get('reason')}")
        if len(str(err.get("reason"))) > 400:
            failures.append("bot_error 太长，界面上放不下")
    shutil.rmtree(err_dir, ignore_errors=True)

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nSUPERVISOR OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
