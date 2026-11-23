"""校验后台任务守护：异常退出的自动重建，正常返回的不重建。

背景：`_message_worker` / `_periodic_message_loop` / `_heartbeat_loop` /
`_credential_watch_loop` 都是 `asyncio.create_task()` 起的**没人 await** 的任务。
里面抛一次异常它们就永久消失，而进程、心跳、WebUI 状态全都还显示「运行中」。
最要命的是 `_message_worker`：它一死，机器人在直播间里再也不说一句话
（感谢、关键词回复、签到全部静默失效）—— 用户看到的是「开着但没反应」。

用法: ./.venv/Scripts/python.exe scripts/check_bg_guard.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/bg_guard_test")


def build_config() -> str:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load((ROOT / "rooms" / "1946287911" / "config.yaml").read_text(encoding="utf-8"))
    base["storage"] = {"sqlite_path": "bot.db"}
    base["auth"] = {"auto_login": True, "credential_store_path": "cred.json",
                    "qr_poll_seconds": 1.0, "refresh_interval_seconds": 3600}
    path = SCRATCH / "config.yaml"
    path.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(path)


async def main() -> int:
    from app.bot import LiveRobot
    from app.config import load_config

    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    # 用固定配置（该房间的 bot.db 只是数据文件路径，这里会被 tmp 覆盖）
    config = load_config(build_config())
    robot = LiveRobot(config=config)

    # 这些工厂在测试里全部换成可观察的桩：被重建时就 +1
    started: dict[str, int] = {}

    def mk(name: str, behaviour: str):
        async def _factory() -> None:
            started[name] = started.get(name, 0) + 1
            if behaviour == "raise":
                raise RuntimeError(f"{name} 模拟崩溃")
            if behaviour == "return":
                return
            await asyncio.sleep(3600)      # 正常长驻
        return _factory

    # ── 1. 崩溃的任务要被重建 ──
    print("[1] 异常退出的后台任务自动重建")
    for attr, name, how in (
        ("_msg_worker_task", "_message_worker", "raise"),
        ("_periodic_task", "_periodic_message_loop", "return"),
        ("_heartbeat_task", "_heartbeat_loop", "run"),
        ("_credential_task", "_credential_watch_loop", "raise"),
    ):
        setattr(robot, name, mk(name, how))
        setattr(robot, attr, asyncio.create_task(getattr(robot, name)()))
    await asyncio.sleep(0.05)   # 让 raise/return 的那几个先结束

    done = [n for n in started if n in
            ("_message_worker", "_periodic_message_loop", "_credential_watch_loop")]
    print("  已启动过:", started)
    restarted = robot._check_background_tasks()
    print("  重建了:", restarted)

    ok("崩溃的 _message_worker 被重建", "_message_worker" in restarted, restarted)
    ok("崩溃的 _credential_watch_loop 被重建",
       "_credential_watch_loop" in restarted, restarted)
    ok("正常返回的 _periodic_message_loop **不**重建",
       "_periodic_message_loop" not in restarted, restarted)
    ok("还在运行的 _heartbeat_loop 不被打扰", "_heartbeat_loop" not in restarted, restarted)
    ok("重建次数计数 = 2", robot._bg_restarts == 2, robot._bg_restarts)
    ok("重建后任务处于运行状态",
       not getattr(robot, "_msg_worker_task").done(), "msg worker should be alive")

    # ── 2. 连续检查不会重复重建同一个任务 ──
    print("[2] 幂等：再次检查不应重复重建")
    before = robot._bg_restarts
    restarted2 = robot._check_background_tasks()
    print("  第二次重建了:", restarted2, "计数:", robot._bg_restarts)
    ok("第二次没有任何重建", restarted2 == [], restarted2)
    ok("计数没有变化", robot._bg_restarts == before, (before, robot._bg_restarts))

    # ── 3. 新建的任务再崩，还能再重建（不是只救一次） ──
    print("[3] 能救第二次")
    getattr(robot, "_msg_worker_task").cancel()
    try:
        await getattr(robot, "_msg_worker_task")
    except asyncio.CancelledError:
        pass
    # 换成一个会崩的版本，模拟第二次崩溃
    setattr(robot, "_message_worker", mk("_message_worker", "raise"))
    setattr(robot, "_msg_worker_task",
            asyncio.create_task(getattr(robot, "_message_worker")()))
    await asyncio.sleep(0.05)
    restarted3 = robot._check_background_tasks()
    ok("第二次崩溃后又被重建", "_message_worker" in restarted3, restarted3)
    ok("计数继续增长", robot._bg_restarts >= 3, robot._bg_restarts)

    # ── 3b. 必现故障的任务不能无限重建（限流）──
    print("[3b] 反无限重建（必现故障）")
    from app.bot import _BG_MAX_RESTARTS

    # 每一轮：造一个「必然崩溃」的 msg worker，等它崩，再巡检
    robot._bg_restart_hist.clear()
    robot._bg_dead.clear()
    rebuilt = 0
    for _ in range(_BG_MAX_RESTARTS + 4):
        setattr(robot, "_message_worker", mk("_message_worker", "raise"))
        crashed = asyncio.create_task(getattr(robot, "_message_worker")())
        setattr(robot, "_msg_worker_task", crashed)
        await asyncio.sleep(0.02)          # 让它崩掉
        before = robot._bg_restarts
        got = robot._check_background_tasks()
        if "_message_worker" in got:
            rebuilt += 1
        await asyncio.sleep(0.01)
    print(f"  {_BG_MAX_RESTARTS + 4} 轮里实际重建 {rebuilt} 次，bg_dead={robot._bg_dead}")
    ok(f"实际重建次数被限流在 {_BG_MAX_RESTARTS} 次", rebuilt == _BG_MAX_RESTARTS, rebuilt)
    ok("超出限流后标记为 bg_dead", "_message_worker" in robot._bg_dead, robot._bg_dead)
    # 已经放弃后，再崩也不该重建
    setattr(robot, "_msg_worker_task", asyncio.create_task(getattr(robot, "_message_worker")()))
    await asyncio.sleep(0.02)
    after_dead = robot._check_background_tasks()
    ok("放弃后不再重建", "_message_worker" not in after_dead, after_dead)

    # ── 4. 心跳里能看出「曾经活着但不说话」──
    print("[4] 心跳暴露 bg_restarts")
    robot.write_heartbeat()
    hb_path = Path(config.storage.sqlite_path).parent / "bot_status.json"
    hb = json.loads(hb_path.read_text(encoding="utf-8"))
    print("  心跳:", {k: hb.get(k) for k in ("bg_restarts", "dropped_stats",
                                             "dropped_no_uid", "phase")})
    ok("心跳含 bg_restarts", hb.get("bg_restarts") == robot._bg_restarts, hb.get("bg_restarts"))
    ok("心跳含 bg_dead（哪项功能彻底停摆）", isinstance(hb.get("bg_dead"), list), hb.get("bg_dead"))

    # ── 5. 结构断言：守护循环里登记的任务清单和 run() 里创建的一致 ──
    print("[5] 任务清单完整性")
    src = (ROOT / "app" / "bot.py").read_text(encoding="utf-8")
    for attr, factory in LiveRobot._BG_TASKS:
        ok(f"{attr} → {factory} 两者都在源码里",
           attr in src and factory in src, (attr, factory))
    ok("_bg_guard_task 在 run() 里被创建", "_bg_guard_task = asyncio.create_task" in src)
    ok("_bg_guard_task 在 shutdown 里被取消", '"_bg_guard_task"' in src)
    # 守护循环自己不能被自己守护（否则无限套娃）
    ok("守护任务不在被守护清单里",
       all(attr != "_bg_guard_task" for attr, _ in LiveRobot._BG_TASKS))

    for name in ("_msg_worker_task", "_periodic_task", "_heartbeat_task",
                 "_credential_task", "_bg_guard_task"):
        task = getattr(robot, name, None)
        if task is not None and not task.done():
            task.cancel()

    robot.store.close()

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(" -", f)
        shutil.rmtree(SCRATCH, ignore_errors=True)
        return 1
    print("\nBG GUARD OK")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
