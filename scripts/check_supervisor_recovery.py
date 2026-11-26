"""守护进程的**重启失败**必须显示在界面上。

用户的原始反馈是「机器人在 win 上会意外关闭，点一下重启就恢复」。能自愈的
情况已经修过多轮；这轮补的是**自愈失败**那半边：

以前 `_recover_room()` 里 `restart_room_async()` 返回 False 时只打一行日志：
    supervisor: room <id> restart failed
界面上用户看到的是「进程已退出」+ 崩溃次数一直涨，15 分钟后变成
「已暂停自动重启」—— 从头到尾不知道是配置写错、依赖缺失还是端口被占。

重启失败是**确定性**的（不是网络抖动），所以原因必须一直挂在 crash_info 上。

本脚本断言：
  [1] 房间已经死了 + desired=running → 守护进程会调 _recover_room
  [2] 重启成功 → 写 crash_info（说已自动重启）
  [3] **重启失败 → crash_info 里带出 bot_error 里的具体原因**
  [4] 重启抛异常 → crash_info 里带出异常
  [5] 连续崩溃超限 → paused=True（前端据此改文案）

    ./.venv/Scripts/python.exe scripts/check_supervisor_recovery.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROOM = "1946287911"


def main() -> int:
    import app.web.server as srv
    from app import process_manager as pm

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="suprec_"))
    try:
        pm.set_rooms_base_dir(str(tmp))
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (tmp / "data").mkdir(parents=True)
        (tmp / "config.yaml").write_text(example, encoding="utf-8")
        room = tmp / "rooms" / ROOM
        (room / "data").mkdir(parents=True)
        (room / "config.yaml").write_text(example, encoding="utf-8")

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._restart_attempts.clear()
        srv._zombie_restarts.clear()
        srv._no_heartbeat_since.clear()

        # desired = 这个房间应该运行
        (tmp / "data").mkdir(parents=True, exist_ok=True)
        (tmp / "data" / "desired_states.json").write_text(
            json.dumps({ROOM: True}), encoding="utf-8")

        def crash_reason() -> str:
            p = srv._crash_info_path(ROOM)
            if not p.exists():
                return ""
            return str(json.loads(p.read_text(encoding="utf-8")).get("reason") or "")

        def crash_data() -> dict:
            p = srv._crash_info_path(ROOM)
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

        # ── [1][2] 重启成功 ──
        print("[1][2] 房间已死 + 应运行 → 自动重启成功")
        calls = {"n": 0}

        async def ok_restart(rid: str) -> bool:
            calls["n"] += 1
            return True

        orig_status, orig_restart = srv.room_status, srv.restart_room_async
        srv.room_status = lambda rid: "stopped"
        srv.restart_room_async = ok_restart
        try:
            asyncio.run(srv._supervise_once())
        finally:
            srv.room_status, srv.restart_room_async = orig_status, orig_restart
        ok("调用了重启", calls["n"] == 1, calls["n"])
        ok("写了 crash_info（用户能看到「已自动重启」）", bool(crash_reason()), crash_reason()[:80])

        # ── [3] 重启失败：原因必须带出来 ──
        print("[3] 重启失败 → crash_info 必须带出 bot_error 的具体原因")
        srv._restart_attempts.clear()
        srv._crash_info_path(ROOM).unlink(missing_ok=True)
        # 模拟一次「启动失败并写下原因」：这正是配置写错时 start_room 的行为
        srv._bot_error_path(ROOM).parent.mkdir(parents=True, exist_ok=True)
        srv._bot_error_path(ROOM).write_text(json.dumps({
            "ts": int(time.time()),
            "reason": "配置文件 rooms/1946287911/config.yaml：`anchor_uid`（主播 UID）必须是数字，当前是 'abc'",
        }, ensure_ascii=False), encoding="utf-8")

        async def bad_restart(rid: str) -> bool:
            return False

        srv.room_status = lambda rid: "stopped"
        srv.restart_room_async = bad_restart
        try:
            asyncio.run(srv._supervise_once())
        finally:
            srv.room_status, srv.restart_room_async = orig_status, orig_restart
        reason = crash_reason()
        print(f"  crash_info.reason = {reason[:110]}")
        ok("crash_info 说明了「自动重启失败」", "自动重启失败" in reason, reason[:80])
        ok("带出了字段名 anchor_uid", "anchor_uid" in reason, reason[:120])
        ok("带出了当前值 abc", "abc" in reason, reason[:120])

        # ── [4] 重启抛异常 ──
        print("[4] 重启抛异常 → crash_info 带出异常")
        srv._restart_attempts.clear()
        srv._crash_info_path(ROOM).unlink(missing_ok=True)
        srv._bot_error_path(ROOM).unlink(missing_ok=True)

        async def boom_restart(rid: str) -> bool:
            raise RuntimeError("port 8000 already in use")

        srv.room_status = lambda rid: "stopped"
        srv.restart_room_async = boom_restart
        try:
            asyncio.run(srv._supervise_once())
        finally:
            srv.room_status, srv.restart_room_async = orig_status, orig_restart
        reason = crash_reason()
        print(f"  crash_info.reason = {reason[:110]}")
        ok("带出了异常信息", "port 8000" in reason or "自动重启异常" in reason, reason[:100])

        # ── [5] 连续崩溃超限 → paused ──
        print("[5] 连续崩溃超限 → paused=True")
        srv._restart_attempts.clear()
        srv._crash_info_path(ROOM).unlink(missing_ok=True)
        now = time.time()
        srv._restart_attempts[ROOM] = [now - i for i in range(10)]
        srv.room_status = lambda rid: "stopped"
        srv.restart_room_async = bad_restart
        try:
            asyncio.run(srv._supervise_once())
        finally:
            srv.room_status, srv.restart_room_async = orig_status, orig_restart
        data = crash_data()
        print(f"  crash_info = {{'paused': {data.get('paused')}, 'reason': {str(data.get('reason'))[:70]!r}}}")
        ok("paused=True（前端据此显示「已暂停自动重启」）", data.get("paused") is True, data.get("paused"))
        ok("有 retry_at（告诉用户什么时候会再试）", int(data.get("retry_at") or 0) > 0, data.get("retry_at"))
    finally:
        import app.web.server as s2
        s2._restart_attempts.clear()
        s2._zombie_restarts.clear()
        s2._no_heartbeat_since.clear()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("SUPERVISOR RECOVERY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
