"""运行期间的保留期清理（不能只在启动时做一次）。

`cleanup_old_data()` 原来只在 `LiveRobot.__init__` 里调用。机器人的正常形态是
「开一次跑几个星期甚至几个月」，于是：

- 用户把「礼物保留天数」改成 90 天后，要等下次重启才会真的删；
- 默认 733 天，跑两年不重启就一直不清理，库一直涨。

而且清理不能在事件循环里同步跑：大库上的 DELETE + 重算聚合是秒级，
exe 模式下所有房间的机器人会一起哑 —— 用户会误以为「点了统计机器人就坏了」。

这里直接构造一个 LiveRobot（真的 StatsStore），把 `_retention_tick()` 跑几次，
验证「每天最多一次 / 真的删了 / 没卡住事件循环」。

    ./.venv/Scripts/python.exe scripts/check_retention_schedule.py
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

UID = 4242


def build_cfg(tmp: Path, gift_days: int, danmaku_days: int):
    import yaml
    src = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    src["room_display_id"] = 1946287911
    src["anchor_uid"] = 8888
    src["storage"] = {"sqlite_path": str(tmp / "data" / "bot.db")}
    src["runtime"] = dict(src.get("runtime") or {}, timezone="Asia/Shanghai", log_level="INFO")
    f = src.setdefault("features", {})
    f["gift_retention_days"] = gift_days
    f["danmaku_retention_days"] = danmaku_days
    f["danmaku_log_enabled"] = True
    f["thanks_enabled"] = False
    cfg = tmp / "config.yaml"
    cfg.write_text(yaml.safe_dump(src, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return cfg


def main() -> int:
    from app.bot import LiveRobot
    from app.config import load_config
    from app.storage import GiftEvent

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="retention_"))
    try:
        (tmp / "data").mkdir(parents=True, exist_ok=True)
        cfg = load_config(str(build_cfg(tmp, gift_days=10, danmaku_days=3)))

        class _FakeAuth:
            status = "ok"

            def write_status(self, *a, **k) -> None:
                pass

            def mark_needs_login(self, *a, **k) -> None:
                pass

        robot = LiveRobot(config=cfg, credential=object(), auth=_FakeAuth())
        store = robot.store

        # 造数据：30 天前 3 条（超过 10 天保留期）+ 今天 2 条
        now = int(time.time())
        old_ts = now - 30 * 86400
        for i in range(3):
            store.record_gift_event(GiftEvent(
                ts=old_ts, month=time.strftime("%Y-%m", time.localtime(old_ts)),
                uid=UID, uname="旧数据", event_type="SEND_GIFT", gift_name="辣条",
                gift_num=1, is_blind_box=0, blind_box_cost=0, actual_value=1,
                profit_value=0, raw_json=json.dumps({"total_coin": 100})), commit=False)
        for i in range(2):
            store.record_gift_event(GiftEvent(
                ts=now, month=time.strftime("%Y-%m", time.localtime(now)),
                uid=UID, uname="新数据", event_type="SEND_GIFT", gift_name="辣条",
                gift_num=1, is_blind_box=0, blind_box_cost=0, actual_value=1,
                profit_value=0, raw_json=json.dumps({"total_coin": 100})), commit=False)
        store._conn.commit()
        before = store._conn.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
        print(f"  造了 {before} 条（3 条过期 / 2 条今天）")
        ok("构造数据 5 条", before == 5, before)

        print("[1] 第一轮 tick：还没到 24 小时，什么都不该删")
        asyncio.run(robot._retention_tick())
        mid = store._conn.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
        ok("没有提前清理", mid == before, f"{before} -> {mid}")

        print("[2] 把上次时间推到 25 小时前，再 tick 一次")
        robot._last_retention_ts = time.time() - 25 * 3600
        t0 = time.perf_counter()
        asyncio.run(robot._retention_tick())
        cost = time.perf_counter() - t0
        after = store._conn.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
        ok("过期记录被删掉了", after == 2, f"{mid} -> {after}")
        ok(f"用了 {cost*1000:.0f}ms（不阻塞事件循环靠 to_thread）", cost < 5.0, f"{cost:.2f}s")
        tss = [r[0] for r in store._conn.execute("SELECT ts FROM gift_events")]
        ok("留下的都是今天的", all(abs(t - now) < 120 for t in tss), tss)

        print("[3] 聚合跟着重算（不能比明细大）")
        agg = store._conn.execute(
            "SELECT gift_count FROM monthly_gift_stats").fetchone()
        ok("月度聚合 = 2", agg and int(agg[0]) == 2, tuple(agg) if agg else None)
        rep = store.consistency_report()
        bad = [r for r in (rep.get("checks") or []) if not r.get("ok")]
        ok("consistency_report 全部 ok", not bad, bad[:1])

        print("[4] 立刻再 tick 一次：不该重复干活")
        stamp = robot._last_retention_ts
        asyncio.run(robot._retention_tick())
        ok("24 小时内不再跑（时间戳没变）", robot._last_retention_ts == stamp)

        print("[5] 保留天数 = 0 表示永久保留，不该删任何东西")
        cfg0 = load_config(str(build_cfg(tmp, gift_days=0, danmaku_days=0)))
        robot0 = LiveRobot(config=cfg0, credential=object(), auth=_FakeAuth())
        robot0._last_retention_ts = 0.0
        n_before = robot0.store._conn.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
        asyncio.run(robot0._retention_tick())
        n_after = robot0.store._conn.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
        ok("0 天 = 不清理", n_before == n_after, f"{n_before} -> {n_after}")

        print("[6] 后台巡检循环里真的接上了（结构断言）")
        src = (ROOT / "app" / "bot.py").read_text(encoding="utf-8")
        seg = src[src.index("async def _background_guard_loop"):]
        seg = seg[: seg.index("async def _retention_tick")]
        ok("guard loop 每轮调用 _retention_tick", "_retention_tick()" in seg)
        ok("清理走 to_thread（不会卡住事件循环）",
           "asyncio.to_thread" in src[src.index("async def _retention_tick"):][:2500])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("RETENTION SCHEDULE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
