"""弹幕连接假死（silent death）检测。

用户原话：「机器人开着，但什么都不收，点一下重启就好了」。
这类故障里进程活着、心跳文件在更新、`danmaku_connected` 还是 true，
守护进程因此永远不重启 —— 只有重启才能救。

B站 服务端每 ~30 秒回一次心跳包，库把它 dispatch 成 `VIEW` 事件，
所以**健康的连接至少每 30 秒会踩一次 `_on_all_events`**。
本脚本断言这条链路和它的自愈逻辑都在：

  [1] `_on_all_events` 最前面记录 `_last_event_ts`（必须早于 PK/噪音的 return）
  [2] 健康连接 → 不重连
  [3] 任务已结束 → 重连
  [4] 对象在、任务没了（半截状态）→ 重连
  [5] 任务活着但 180s 没事件 → 重连（这就是假死）
  [6] 未开播时不判假死（不收数据是正常的）
  [7] `_start_danmaku` 失败不能留下半截状态
  [8] 心跳里有 `event_silent_seconds` 供 WebUI 显示

    ./.venv/Scripts/python.exe scripts/check_danmaku_liveness.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SILENCE = 180.0


def make_bot():
    """造一个不跑网络的 LiveRobot 外壳，只用来调判定逻辑。"""
    from app.bot import LiveRobot

    bot = LiveRobot.__new__(LiveRobot)   # 不走 __init__（会连 B站/建库）
    bot._danmaku = object()              # 有连接对象
    bot._danmaku_task = None
    bot._is_live = True
    bot._last_event_ts = 0.0
    return bot


class FakeTask:
    """假的 asyncio.Task：只实现判定用到的三个方法。"""

    def __init__(self, done: bool = False, cancelled: bool = False, exc=None) -> None:
        self._done = done
        self._cancelled = cancelled
        self._exc = exc

    def done(self) -> bool:
        return self._done

    def cancelled(self) -> bool:
        return self._cancelled

    def exception(self):
        if self._exc is None:
            raise asyncio.CancelledError
        return self._exc


def main() -> int:
    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    from app.bot import _DANMAKU_SILENCE_TIMEOUT as TIMEOUT
    src = (ROOT / "app/bot.py").read_text(encoding="utf-8")

    print("[1] 事件时间戳必须记在 _on_all_events 最前面")
    i_bump = src.index("self._bump_event_count(event_type)")
    # 从 _bump 之后找：文件里还有一处 `_last_event_ts = time.time()`
    # 在 _start_danmaku 里（给宽限期），不能把那一处当成这里的
    i_stamp = src.index("self._last_event_ts = time.time()", i_bump)
    i_pk = src.index('if event_type.startswith("PK_")')
    i_noisy = src.index('noisy_prefixes = (')
    ok("记录语句存在", i_stamp > 0)
    ok("记录早于 PK 分支的 return", i_stamp < i_pk)
    ok("记录早于噪音过滤的 return", i_stamp < i_noisy)
    ok("贴着 _bump_event_count", 0 < i_stamp - i_bump < 400, f"间隔 {i_stamp - i_bump} 字符")

    print("[2] 健康连接不重连")
    bot = make_bot()
    bot._danmaku_task = FakeTask()
    bot._last_event_ts = time.time()          # 刚收到事件
    ok("刚收到事件 -> 健康", bot._danmaku_health_action() is None, str(bot._danmaku_health_action()))
    bot._last_event_ts = time.time() - 60     # 一个心跳周期内
    ok("60 秒前收到 -> 仍健康", bot._danmaku_health_action() is None)

    print("[3] 任务已结束 -> 重连")
    bot = make_bot()
    bot._danmaku_task = FakeTask(done=True, cancelled=False, exc=RuntimeError("ws closed"))
    reason = bot._danmaku_health_action()
    ok("任务结束会触发重连", reason is not None, reason)
    ok("原因里带异常信息", "ws closed" in (reason or ""), reason)
    bot._danmaku_task = FakeTask(done=True, cancelled=True)
    ok("被 cancel 的任务也算结束", bot._danmaku_health_action() is not None)

    print("[4] 对象在、任务没了（半截状态）-> 重连")
    bot = make_bot()
    bot._danmaku_task = None
    reason = bot._danmaku_health_action()
    ok("半截状态会触发重连", reason is not None, reason)
    ok("文案说清楚是任务不存在", "任务不存在" in (reason or ""), reason)

    print("[5] 任务活着但长时间无事件（假死）-> 重连")
    bot = make_bot()
    bot._danmaku_task = FakeTask()
    bot._last_event_ts = time.time() - (TIMEOUT + 10)
    reason = bot._danmaku_health_action()
    ok("超过阈值会触发重连", reason is not None, reason)
    ok("文案是「假死」", "假死" in (reason or ""), reason)
    bot._last_event_ts = time.time() - (TIMEOUT - 10)
    ok("阈值内不误杀", bot._danmaku_health_action() is None)

    print("[6] 未开播时不判假死")
    bot = make_bot()
    bot._is_live = False
    bot._danmaku_task = FakeTask()
    bot._last_event_ts = time.time() - 9999
    ok("下播期间不收数据是正常的", bot._danmaku_health_action() is None)

    print("[7] _start_danmaku 失败不留半截状态")
    ok("建 task 失败时清空 _danmaku",
       "self._danmaku = None" in src and "self._danmaku_task = asyncio.create_task(_run_connect())" in src)
    i_try = src.index("try:\n            self._danmaku_task = asyncio.create_task(_run_connect())")
    i_clean = src.index("self._danmaku = None", i_try)
    ok("清理语句在 except 分支里", i_clean > i_try, f"offset {i_clean - i_try}")
    ok("record_stream_date 被 try 保护", "record_stream_date failed" in src)

    print("[8] 心跳暴露 event_silent_seconds")
    ok("心跳字段存在", '"event_silent_seconds":' in src)
    ok("没有事件时给 -1（区分「刚启动」和「很久没数据」）",
       'getattr(self, "_last_event_ts", 0) else -1' in src)

    print("[9] guard 循环必须真的用这个判定（光有 predicated 不算）")
    loop_src = src[src.index("async def _danmaku_guard_loop"):]
    loop_src = loop_src[:loop_src.index("async def ", 10)]
    ok("循环里调用了 _danmaku_health_action()", "_danmaku_health_action()" in loop_src)
    i_call = loop_src.index("_danmaku_health_action()")
    i_done = loop_src.find("task.done()")
    ok("没有「task 没结束就 continue」的短路",
       i_done == -1 or i_done > i_call,
       f"task.done() 出现在第 {i_done} 字符，health_action 在第 {i_call}")

    print("[10] 端到端：假死时 guard 循环真的会重连")
    import app.bot as botmod

    async def integration() -> dict:
        b = make_bot()
        b.logger = __import__("logging").getLogger("test")   # 属性名就是 logger
        b._danmaku_task = FakeTask()                # 任务活着
        b._last_event_ts = time.time() - (TIMEOUT + 5)   # 但很久没事件
        calls = {"n": 0}

        async def fake_restart() -> None:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise asyncio.CancelledError

        b._restart_danmaku = fake_restart   # type: ignore[assignment]

        old_interval = botmod._DANMAKU_GUARD_INTERVAL
        botmod._DANMAKU_GUARD_INTERVAL = 0.01
        task = asyncio.create_task(b._danmaku_guard_loop())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        botmod._DANMAKU_GUARD_INTERVAL = old_interval
        return calls

    calls = asyncio.run(integration())
    ok("假死连接被重连（不是只判定不动作）", calls["n"] >= 1, f"重连 {calls['n']} 次")

    print("[11] 凭证热更新必须重建连接（只换对象对已开的连接无效）")
    i_swap = src.index("async def _swap_credential")
    swap_src = src[i_swap:src.index("async def shutdown", i_swap)]
    ok("_swap_credential 是 async", swap_src.lstrip().startswith("async def"))
    ok("换完凭证会重建弹幕连接", "_restart_danmaku()" in swap_src, swap_src[:200])
    ok("调用点有 await", "await self._swap_credential(" in src)
    ok("说明为什么必须重连（库的鉴权只在 connect 时发一次）",
       "connect()` 里用" in swap_src or "连_connect" in swap_src or "一次性完成" in swap_src)

    print("[12] 重连必须串行（guard / 凭证 / poll 三处会撞）")
    ok("有 _restart_lock", "def _restart_lock" in src)
    ok("_restart_danmaku 走锁", "async with self._restart_lock()" in src)
    ok("__init__ 里声明了锁字段", "_danmaku_restart_lock: Optional[asyncio.Lock] = None" in src)

    print("[9b] 阈值合理性")
    ok("阈值大于 3 个心跳周期（90s，避免误杀）", TIMEOUT > 90, f"{TIMEOUT}s")
    ok("阈值小于 10 分钟（用户能忍的自愈时间）", TIMEOUT <= 600, f"{TIMEOUT}s")
    ok("guard 每 60 秒检查一次", "_DANMAKU_GUARD_INTERVAL = 60.0" in src)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("DANMAKU LIVENESS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
