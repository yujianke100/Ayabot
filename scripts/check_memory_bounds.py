"""校验长期运行的内存上限：AI 上下文表与以 uid 为键的冷却表必须封顶。

背景（真 bug）：机器人设计上要连续跑几个月，但这些表全都按 uid 记录、从不清理：
  - `_chat_contexts`：每人一份对话历史。实测 1 万 UID × 10 条 ≈ 20MB，
    一个热闹直播间跑半个月就能上百 MB。
  - `_last_thanks_ts` / `_keyword_reply_cooldown_ts` / `_last_welcome_ts` / `_like_counts`
更糟的是 `max_messages` 配成 0 时 `history[-0:]` 等于 `history[0:]`（整表返回），
列表会越滚越大且永不收敛（实测 50 → 55 → 一直涨），负数同理。
内存涨到一定程度在 Windows 上就是 MemoryError → 进程退出，
用户看到的是「机器人莫名其妙关了」——和「统计全 0」一样属于静默失败。

用法: ./.venv/Scripts/python.exe scripts/check_memory_bounds.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/memory_bounds_test")


def build_config() -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load(
        (ROOT / "rooms" / "1946287911" / "config.yaml").read_text(encoding="utf-8"))
    base["storage"] = {"sqlite_path": "bot.db"}
    base["auth"] = {"auto_login": True, "credential_store_path": "cred.json",
                    "qr_poll_seconds": 1.0, "refresh_interval_seconds": 3600}
    path = SCRATCH / "config.yaml"
    path.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path


async def main() -> int:
    from app import bot as bot_mod
    from app.bot import (LiveRobot, _MAX_CHAT_CONTEXTS, _MAX_CONTEXT_MESSAGES,
                         _MAX_TRACKED_UIDS)
    from app.config import load_config
    from app.web import server as srv

    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    robot = LiveRobot(config=load_config(str(build_config())))
    original_llm = dict(srv._LLM_CONFIG_DICT)

    def set_ctx(**kw):
        srv._LLM_CONFIG_DICT.clear()
        srv._LLM_CONFIG_DICT.update({"context": kw})

    # ── 1. max_messages 边界：0 / 负数 不能无限增长 ──
    print("[1] max_messages 边界")
    for cfg_value, label in ((0, "0"), (-5, "-5"), ("abc", "非数字")):
        set_ctx(enabled=True, content="all", mode="isolated", max_messages=cfg_value)
        robot._chat_contexts.clear()
        for i in range(80):
            robot._record_chat_context(f"弹幕{i}", "甲", 123)
        size = len(robot._chat_contexts.get(123, []))
        print(f"  max_messages={label:<6} → 上下文条数 {size}")
        ok(f"max_messages={label} 时上下文不会无限涨（≤ {_MAX_CONTEXT_MESSAGES}）",
           size <= _MAX_CONTEXT_MESSAGES, size)

    # 正常值仍要按配置生效
    set_ctx(enabled=True, content="all", mode="isolated", max_messages=5)
    robot._chat_contexts.clear()
    for i in range(20):
        robot._record_chat_context(f"弹幕{i}", "甲", 456)
    ok("max_messages=5 时只留 5 条", len(robot._chat_contexts[456]) == 5,
       len(robot._chat_contexts[456]))

    # ── 2. 上下文表自身封顶（按最久未用淘汰）──
    print("[2] 上下文表封顶")
    set_ctx(enabled=True, content="all", mode="isolated", max_messages=3)
    robot._chat_contexts.clear()
    total = _MAX_CHAT_CONTEXTS + 300
    for uid in range(100000, 100000 + total):
        robot._record_chat_context("你好", "甲", uid)
    print(f"  写入 {total} 个不同 UID → 表内 {len(robot._chat_contexts)}")
    ok(f"表内不超过 {_MAX_CHAT_CONTEXTS}", len(robot._chat_contexts) <= _MAX_CHAT_CONTEXTS,
       len(robot._chat_contexts))
    ok("最早的 UID 已被淘汰", 100000 not in robot._chat_contexts)
    ok("最新的 UID 仍在", (100000 + total - 1) in robot._chat_contexts)

    # 活跃会话不该被淘汰（读也刷新 LRU）
    robot._chat_contexts.clear()
    for uid in range(200000, 200000 + _MAX_CHAT_CONTEXTS):
        robot._record_chat_context("hi", "甲", uid)
    hot = 200000                      # 最老的那个
    robot._record_chat_context("我还活着", "甲", hot)   # 触它 → move_to_end
    for uid in range(300000, 300000 + 50):
        robot._record_chat_context("新人", "乙", uid)  # 挤掉 50 个最老的
    print(f"  活跃用户 {hot} 是否还在: {hot in robot._chat_contexts}")
    ok("刚说过话的老用户不会被淘汰", hot in robot._chat_contexts)

    # ── 3. 标量表封顶，且丢的是最旧的 ──
    print("[3] 冷却表封顶")
    now = time.time()
    for uid in range(_MAX_TRACKED_UIDS + 400):
        robot._last_thanks_ts[uid] = now - (_MAX_TRACKED_UIDS + 400 - uid)
        robot._keyword_reply_cooldown_ts[uid] = now - uid
        robot._last_welcome_ts[uid] = now - uid
        robot._like_counts[uid] = 3
    trimmed = robot._trim_tracking_tables()
    print(f"  清理结果: {trimmed}")
    ok("清理后不超过上限",
       all(len(t) <= _MAX_TRACKED_UIDS for t in (
           robot._last_thanks_ts, robot._keyword_reply_cooldown_ts,
           robot._last_welcome_ts, robot._like_counts)),
       {n: len(t) for n, t in (("thanks", robot._last_thanks_ts),
                               ("kw", robot._keyword_reply_cooldown_ts),
                               ("welcome", robot._last_welcome_ts),
                               ("like", robot._like_counts))})
    ok("丢的是时间戳最小的（最久没触发）", 0 not in robot._last_thanks_ts)
    ok("最近触发的保留",
       (_MAX_TRACKED_UIDS + 399) in robot._last_thanks_ts)
    ok("清理函数返回了每张表的条数", len(trimmed) >= 4, trimmed)

    # 没超上限时不该乱动（避免误删冷却状态）
    robot._last_thanks_ts.clear()
    robot._last_thanks_ts[1] = now
    before = len(robot._last_thanks_ts)
    robot._trim_tracking_tables()
    ok("未超限时不清理", len(robot._last_thanks_ts) == before, len(robot._last_thanks_ts))

    # ── 4. 守护循环真的会调用清理 ──
    print("[4] 守护循环调用清理")
    src = (ROOT / "app" / "bot.py").read_text(encoding="utf-8")
    seg = src[src.index("async def _background_guard_loop"):]
    seg = seg[:seg.index("def _check_background_tasks")]
    ok("_background_guard_loop 里调用了 _trim_tracking_tables",
       "_trim_tracking_tables()" in seg)

    # ── 5. 单条消息也有上限（配置写大也夹住）──
    print("[5] 单条上下文长度夹取")
    ok(f"_MAX_CONTEXT_MESSAGES = {_MAX_CONTEXT_MESSAGES} 合理（5~200）",
       5 <= _MAX_CONTEXT_MESSAGES <= 200, _MAX_CONTEXT_MESSAGES)
    ok(f"_MAX_TRACKED_UIDS = {_MAX_TRACKED_UIDS} 合理（500~20000）",
       500 <= _MAX_TRACKED_UIDS <= 20000, _MAX_TRACKED_UIDS)
    ok(f"_MAX_CHAT_CONTEXTS = {_MAX_CHAT_CONTEXTS} 合理（100~5000）",
       100 <= _MAX_CHAT_CONTEXTS <= 5000, _MAX_CHAT_CONTEXTS)

    srv._LLM_CONFIG_DICT.clear()
    srv._LLM_CONFIG_DICT.update(original_llm)
    robot.store.close()

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(" -", f)
        shutil.rmtree(SCRATCH, ignore_errors=True)
        return 1
    print("\nMEMORY BOUNDS OK")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
