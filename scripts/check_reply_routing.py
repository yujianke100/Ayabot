"""校验回复入队的去重键：必须按 (内容, 收件人) 而不是只用内容。

背景（真 bug）：`_pending_texts` 以前是 `set[str]`，只用文案做去重键。
两个不同用户在同一时间窗口触发同一条回复时（最典型：两人都说 #盲盒统计
且都没有盲盒记录 → 文案同为「无盲盒记录」），**只有第一个人收得到**，
第二个人彻底没反应。用户视角就是「机器人不回复我」。

在修复前，三个人同时触发只会入队 1 条。

用法: ./.venv/Scripts/python.exe scripts/check_reply_routing.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/reply_routing_test")


def build_config(**rate_limit) -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load(
        (ROOT / "rooms" / "1946287911" / "config.yaml").read_text(encoding="utf-8"))
    base["storage"] = {"sqlite_path": "bot.db"}
    base["auth"] = {"auto_login": True, "credential_store_path": "cred.json",
                    "qr_poll_seconds": 1.0, "refresh_interval_seconds": 3600}
    merged = {"reply_delay_seconds": 0.15, "send_interval_seconds": 0.1,
              "max_queue_size": 50, "max_text_len": 30}
    merged.update(rate_limit)
    base["rate_limit"] = dict(base.get("rate_limit") or {}, **merged)
    path = SCRATCH / "config.yaml"
    path.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path


async def main() -> int:
    from app.bot import LiveRobot
    from app.config import load_config

    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    cfg = load_config(str(build_config()))
    robot = LiveRobot(config=cfg)
    q = robot._msg_queue

    def drain() -> list[tuple[str, int | None]]:
        out = [(m.text, m.reply_uid) for m in list(q._queue)]
        while not q.empty():
            q.get_nowait()
        return out

    # ── 1. 不同用户、同一文案：都要入队 ──
    print("[1] 三个用户触发同一句回复")
    await asyncio.gather(
        robot._enqueue_reply("无盲盒记录", reply_uid=1001),
        robot._enqueue_reply("无盲盒记录", reply_uid=2002),
        robot._enqueue_reply("无盲盒记录", reply_uid=3003),
    )
    items = drain()
    print(f"  入队 {len(items)} 条: {items}")
    ok("三个人都收到了（3 条）", len(items) == 3, len(items))
    ok("收件人分别是 1001/2002/3003",
       sorted(r for _, r in items) == [1001, 2002, 3003], items)

    # ── 2. 同一个用户的重复事件：只发一次 ──
    print("[2] 同一用户重复触发（B站重连会重复推送）")
    await asyncio.gather(
        robot._enqueue_reply("无盲盒记录", reply_uid=4004),
        robot._enqueue_reply("无盲盒记录", reply_uid=4004),
    )
    items = drain()
    print(f"  入队 {len(items)} 条: {items}")
    ok("同一用户只入队 1 条", len(items) == 1, len(items))
    ok("收件人是 4004", items and items[0][1] == 4004, items)

    # ── 3. 同一用户、不同文案：都要发 ──
    print("[3] 同一用户的两种不同回复")
    await asyncio.gather(
        robot._enqueue_reply("无盲盒记录", reply_uid=5005),
        robot._enqueue_reply("今日盲盒共3个", reply_uid=5005),
    )
    items = drain()
    print(f"  入队 {len(items)} 条: {items}")
    ok("两条不同文案都发出", len(items) == 2, len(items))

    # ── 4. 欢迎语：模板不含 {uname} 时不同人也都要发 ──
    print("[4] 欢迎语（模板渲染成同一句）")
    robot.config.features.welcome_enabled = True
    robot.config.features.welcome_template = "欢迎来到直播间"
    for uid, uname in ((6006, "甲"), (7007, "乙")):
        ev = {"type": "INTERACT_WORD_V2",
              "data": {"data": {"uid": uid, "uname": uname, "msg_type": 1}}}
        await robot._on_enter_room(ev)
    items = drain()
    print(f"  入队 {len(items)} 条: {items}")
    ok("两个新观众都收到欢迎", len(items) == 2, len(items))

    # ── 5. 队列满时要把占位释放掉，否则该用户之后永远收不到同一句 ──
    print("[5] 队列满后占位必须释放")
    small = LiveRobot(config=load_config(str(build_config(max_queue_size=1))))
    small._apply_keyword_filter = lambda t: t  # type: ignore[assignment]
    # 塞满队列（用 retry_delay=0 直接入队）
    small.config.rate_limit.reply_delay_seconds = 0
    for i in range(1):
        await small._enqueue_reply("占位消息", reply_uid=8008)
    print(f"  队列已满: qsize={small._msg_queue.qsize()}")
    before = len(small._pending_texts)
    await small._enqueue_reply("第二条", reply_uid=9009)   # 会被 QueueFull 拒掉
    print(f"  被拒后 pending={len(small._pending_texts)}（拒绝前 {before}）")
    ok("被拒的消息没有留在 pending 里",
       ("第二条", 9009) not in small._pending_texts, small._pending_texts)
    small.store.close()

    # ── 6. 结构断言：worker 释放键时必须带 reply_uid ──
    print("[6] 结构断言")
    src = (ROOT / "app" / "bot.py").read_text(encoding="utf-8")
    ok("pending 集合的键是 (text, uid) 二元组",
       "set[tuple[str, Optional[int]]]" in src)
    ok("worker 释放时带 reply_uid",
       "self._pending_texts.discard((msg.text, msg.reply_uid))" in src)
    ok("不再有按纯文本判断的旧写法",
       "if text in self._pending_texts:" not in src)

    robot.store.close()

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(" -", f)
        shutil.rmtree(SCRATCH, ignore_errors=True)
        return 1
    print("\nREPLY ROUTING OK")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
