"""守护进程对「卡死」的重启必须和「崩溃」走同一本账。

`_supervise_once` 有两条重启路径：

  A. 进程退出（`room_status != running`）→ `_recover_room()`
     走 `_restart_allowed()`：15 分钟内最多 5 次，超限就**暂停自动重启**并把
     `paused=True` 写进 `crash_info.json`，界面上明确说「已暂停，不会再自己重启」。

  B. 进程活着但卡死（心跳超时 / 从没写过心跳）→ 以前**自己**写一行 crash_info
     然后直接 `restart_room_async()`：既不记入 `_restart_attempts`，也永远不 paused。

于是 B 的后果是：一个修不好的房间（比如每次启动都在同一处卡死）会被
每 10 分钟重启一次，**永远停不下来**，而界面每次都说「守护进程已自动重启」。
用户看到的就是「机器人莫名其妙关了又自己起来，一直这样」——
这正是 B 路径要兜的故障，也是最不该静默循环的那一种。

另外 `crash_info.attempts` 一直是 0（因为 B 不记账），界面上连「第几次」都没有。

    ./.venv/Scripts/python.exe scripts/check_supervisor_zombie.py
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
ROOM2 = "2000000002"


def main() -> int:
    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="supzombie_"))
    try:
        srv._ROOMS_BASE_DIR = str(tmp)
        # **必须两处都设**：server 读状态文件用 `_ROOMS_BASE_DIR`，而
        # `process_manager._read_pid` 用 `set_rooms_base_dir()` 那一份。
        # 只设前者会让 `_room_waiting_login` 去真实 rooms/ 里找 bot.pid ——
        # 测试会去读用户真实数据，而且判定结果是错的（这个坑我自己踩过）。
        from app.process_manager import set_rooms_base_dir
        set_rooms_base_dir(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        for rid in (ROOM, ROOM2):
            d = tmp / "rooms" / rid / "data"
            d.mkdir(parents=True)
            (tmp / "rooms" / rid / "config.yaml").write_text(
                (ROOT / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")

        restarts: list[str] = []

        async def fake_restart(room_id: str) -> bool:
            restarts.append(room_id)
            return True

        srv.restart_room_async = fake_restart          # type: ignore[assignment]
        srv.room_status = lambda rid: "running"        # type: ignore[assignment]
        srv._list_rooms_from_disk = lambda: [          # type: ignore[assignment]
            {"room_id": ROOM}, {"room_id": ROOM2}]

        def reset_state() -> None:
            srv._load_desired_states = lambda: {ROOM: True}  # type: ignore[assignment]
            srv._restart_attempts.clear()
            srv._zombie_restarts.clear()
            srv._no_heartbeat_since.clear()
            srv._clear_crash_state(ROOM)
            (tmp / "rooms" / ROOM / "data" / "crash_info.json").unlink(missing_ok=True)
            (tmp / "rooms" / ROOM / "bot.pid").unlink(missing_ok=True)

        def crash() -> dict:
            p = tmp / "rooms" / ROOM / "data" / "crash_info.json"
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

        def write_hb(payload: dict | None) -> None:
            p = tmp / "rooms" / ROOM / "data" / "bot_status.json"
            if payload is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(json.dumps(payload), encoding="utf-8")

        print("[1] 心跳超时（卡死）会重启，并且**要记进重启账本**")
        reset_state()
        write_hb({"ts": int(time.time()) - 600, "danmaku_task_alive": True})
        asyncio.run(srv._supervise_once())
        ok("确实重启了一次", restarts == [ROOM], restarts)
        ok("crash_info 写的是心跳超时",
           "心跳超时" in (crash() or {}).get("reason", ""), crash())
        ok("**重启次数入账**（以前是 0，界面连第几次都看不到）",
           len(srv._restart_attempts.get(ROOM, [])) == 1,
           srv._restart_attempts.get(ROOM))

        print("[2] 反复卡死 → 必须像崩溃一样「暂停自动重启」")
        reset_state()
        restarts.clear()
        write_hb({"ts": int(time.time()) - 600, "danmaku_task_alive": True})
        for _ in range(6):
            srv._zombie_restarts[ROOM] = 0.0   # 越过 10 分钟冷却，模拟「一直修不好」
            asyncio.run(srv._supervise_once())
        ok("重启次数被封顶（<= 5 次）", len(restarts) <= 5, len(restarts))
        info = crash()
        ok("**crash_info.paused = true**", info.get("paused") is True, info)
        ok("文案说明已暂停", "暂停自动重启" in info.get("reason", ""), info.get("reason"))
        ok("给出了下次重试时间", int(info.get("retry_at") or 0) > int(time.time()),
           info.get("retry_at"))

        print("[3] 「从没写过心跳」的僵尸任务同样要记账")
        reset_state()
        restarts.clear()
        write_hb(None)
        asyncio.run(srv._supervise_once())          # 第一次只是记下起点
        ok("宽限期内不重启", not restarts, restarts)
        srv._no_heartbeat_since[ROOM] = time.time() - 999   # 模拟已过宽限期
        asyncio.run(srv._supervise_once())
        ok("过了宽限期重启一次", restarts == [ROOM], restarts)
        ok("同样入账", len(srv._restart_attempts.get(ROOM, [])) >= 1,
           srv._restart_attempts.get(ROOM))
        ok("crash_info 有疑似僵尸任务", "僵尸" in (crash() or {}).get("reason", ""), crash())

        print("[4] 等扫码登录不算卡死（不能重启）")
        reset_state()
        restarts.clear()
        write_hb({"ts": int(time.time()) - 900, "waiting_login": True,
                  "phase": "waiting_login"})
        (tmp / "rooms" / ROOM / "data" / "auth_status.json").write_text(
            json.dumps({"status": "needs_login", "reason": "x",
                        "pid": __import__("os").getpid()}), encoding="utf-8")
        # `_room_waiting_login` 要求「状态文件里的 pid == 当前进程 pid」，
        # 子进程模式下 pid 来自 bot.pid，所以夹具必须写它。
        (tmp / "rooms" / ROOM / "bot.pid").write_text(
            str(__import__("os").getpid()), encoding="utf-8")
        asyncio.run(srv._supervise_once())
        ok("没有重启", not restarts, restarts)

        print("[5] 心跳新鲜时什么都不做")
        reset_state()
        restarts.clear()
        (tmp / "rooms" / ROOM / "data" / "auth_status.json").unlink(missing_ok=True)
        write_hb({"ts": int(time.time()) - 5, "danmaku_task_alive": True})
        asyncio.run(srv._supervise_once())
        ok("没有重启", not restarts, restarts)
        ok("没有写 crash_info", not crash(), crash())

        print("[6] 结构断言：卡死路径必须复用 _recover_room")
        src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        body = src[src.find("async def _supervise_once"):src.find("async def _supervise_loop")]
        ok("巡检函数里没有裸调用 restart_room_async",
           "await restart_room_async(" not in body,
           [ln.strip() for ln in body.splitlines() if "restart_room_async(" in ln][:3])
        # 两条卡死分支（心跳超时 / 从没写过心跳）都必须走同一个入口
        ok("两条卡死分支都走 _zombie_restart_async",
           body.count("_zombie_restart_async(") == 3,     # 1 def + 2 calls
           body.count("_zombie_restart_async("))
        # 而那个入口必须复用崩溃记账（_recover_room）
        zi = body.find("async def _zombie_restart_async")
        zbody = body[zi:] if zi >= 0 else ""
        ok("卡死入口调用了 _recover_room", "_recover_room(room_id" in zbody,
           zbody[-200:].strip().splitlines()[-1] if zbody else "未找到函数")
        ok("卡死入口保留了 10 分钟冷却（否则重启后旧心跳会立刻再次触发）",
           "_zombie_restarts.get(room_id" in zbody)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("SUPERVISOR ZOMBIE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
