"""出站消息队列的三种边界：队列满、去重键泄漏、任务被取消。

真实故障形态：`_enqueue_reply` 先占去重键 `_pending_texts.add(key)`，
再 `await asyncio.sleep(reply_delay_seconds)`，最后才 put_nowait。
中间那段**可被取消**。一旦在这期间被取消（或抛出 QueueFull 之外的异常），
键就永远留在 `_pending_texts` 里 —— 同一收件人的同一句话以后再也发不出去，
而日志里一条错误都没有。

本脚本断言：
  [1] 正常发送：键由 _message_worker 释放（不是 enqueue 自己放）
  [2] 队列满：键必须还回去，可以再发
  [3] 延迟期间被取消：键必须还回去
  [4] 去重表有上限，不会无限增长

    ./.venv/Scripts/python.exe scripts/check_outbound_queue.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def make_bot(queue_max: int = 50, delay: float = 0.0):
    """只带出站队列所需字段的 LiveRobot 外壳。"""
    from app.bot import LiveRobot

    bot = LiveRobot.__new__(LiveRobot)
    bot.config = SimpleNamespace(
        features=SimpleNamespace(use_chinese_numbers_global=False),
        rate_limit=SimpleNamespace(reply_delay_seconds=delay, max_queue_size=queue_max),
    )
    bot._pending_texts = set()
    bot._msg_queue = asyncio.Queue(maxsize=queue_max)
    bot.logger = __import__("logging").getLogger("q")
    return bot


def main() -> int:
    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    src = (ROOT / "app/bot.py").read_text(encoding="utf-8")

    async def scenario() -> dict:
        out = {}

        # ── [1] 正常路径 ──
        bot = make_bot()
        await bot._enqueue_reply("正常一句话", 100)
        out["normal_q"] = bot._msg_queue.qsize()
        out["normal_pending"] = ("正常一句话", 100) in bot._pending_texts
        # _message_worker 取走后会 discard；这里手动模拟
        msg = await bot._msg_queue.get()
        bot._pending_texts.discard((msg.text, msg.reply_uid))
        out["after_consume"] = ("正常一句话", 100) in bot._pending_texts

        # ── [2] 队列满 ──
        bot2 = make_bot(queue_max=1)
        await bot2._enqueue_reply("先占满", 200)
        await bot2._enqueue_reply("会被丢弃", 200)
        out["full_q"] = bot2._msg_queue.qsize()
        out["full_pending_leak"] = ("会被丢弃", 200) in bot2._pending_texts

        # ── [3] 延迟期间被取消 ──
        bot3 = make_bot(delay=5.0)
        task = asyncio.create_task(bot3._enqueue_reply("取消测试", 300))
        await asyncio.sleep(0.05)          # 让它进到 sleep(delay) 里
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        out["cancel_q"] = bot3._msg_queue.qsize()
        out["cancel_leak"] = ("取消测试", 300) in bot3._pending_texts

        # 取消之后还能不能重新发？
        bot3.config.rate_limit.reply_delay_seconds = 0.0
        await bot3._enqueue_reply("取消测试", 300)
        out["resend_after_cancel_q"] = bot3._msg_queue.qsize()
        return out

    res = asyncio.run(scenario())
    print(f"  结果: {res}")

    print("[1] 正常路径")
    ok("消息进队列", res["normal_q"] == 1, res["normal_q"])
    ok("发送前键在（用于去重）", res["normal_pending"] is True)
    ok("被 worker 取走后键释放", res["after_consume"] is False)

    print("[2] 队列满")
    ok("队列满时不入队", res["full_q"] == 1, res["full_q"])
    ok("**队列满时键被还回去**（还能重试）", res["full_pending_leak"] is False,
       "键留着=这句话永久发不出去")

    print("[3] 延迟期间被取消")
    ok("取消时不入队", res["cancel_q"] == 0, res["cancel_q"])
    ok("**取消时键被还回去**（没有泄漏）", res["cancel_leak"] is False,
       "键留着=同一句话以后永远发不出去，且日志无异常")
    ok("取消后可以重新入队", res["resend_after_cancel_q"] == 1, res["resend_after_cancel_q"])

    print("[4] 结构断言：必须用 finally 兜非成功路径")
    body = src[src.index("async def _enqueue_reply"):]
    body = body[:body.index("def _get_guard_level")]
    ok("有 enqueued 标记", "enqueued = False" in body)
    ok("非成功路径统一 discard", "if not enqueued:" in body and "discard(key)" in body)

    print("[5] 去重表有上限")
    ok("_pending_texts 被 _trim_tracking_tables 管到",
       "_pending_texts" in src[src.index("def _trim_tracking_tables"):src.index("def _check_background_tasks")],
       "否则长时间运行会无限增长（每条外来文本都可能留下一条）")

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("OUTBOUND QUEUE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
