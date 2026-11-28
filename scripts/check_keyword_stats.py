"""「关键词能触发、但统计全是 0」的端到端回归。

用户的原话是「现在礼物统计功能失效，关键词可以触发查看统计，但是所有统计都是 0」。
这句话把范围缩得很小：**关键词链路是通的**（机器人确实回了话），
**数字是 0**。所以要么礼物没进库，要么聚合表没跟着涨，要么回复读的是另一个口径。

这里用一个真的 `LiveRobot` + 真的 `StatsStore`（临时库）跑完整链路：
    礼物事件 → _on_gift → 写 gift_events + monthly_* → 弹幕「#盲盒统计」→ 回复文本
然后断言回复里的数字**不是 0**，并且和写进库的值一致。

同时校验三个口径互相对得上（这正是「某个页面上是 0」的判别方法）：
- gift_events 明细（**关键词回复、WebUI 排行/导出都读它** —— 唯一事实来源）
- monthly_blindbox_stats（增量聚合，供统计自检/重算；漂移也不该影响回复，见 [9]）
- /api/rooms/{id}/ranking（第三个口径）

    ./.venv/Scripts/python.exe scripts/check_keyword_stats.py
"""
from __future__ import annotations

import asyncio
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UID = 75123456
UNAME = "测试观众甲"
ANCHOR = 8888


def build_config(tmp: Path) -> Path:
    """从示例配置出发，改成「单人房间 + 明确的可断言模板」。"""
    import yaml
    src = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    src["room_display_id"] = 1946287911
    src["anchor_uid"] = ANCHOR
    src["storage"] = {"sqlite_path": str(tmp / "data" / "bot.db")}
    src["runtime"] = dict(src.get("runtime") or {}, timezone="Asia/Shanghai", log_level="INFO")
    feats = src.setdefault("features", {})
    feats["use_chinese_numbers"] = False
    feats["use_chinese_numbers_global"] = False
    feats["blindbox_result_monthly"] = "本月盲盒共{count}个，花费{cost}，收益{profit}"
    feats["blindbox_result_today"] = "今日盲盒共{count}个，花费{cost}，收益{profit}"
    feats["blindbox_glassheart_enabled"] = False
    feats["thanks_enabled"] = False
    feats["welcome_enabled"] = False
    feats["keyword_reply"] = {"enabled": False, "cooldown_seconds": 0, "rules": []}
    cfg = tmp / "config.yaml"
    cfg.write_text(yaml.safe_dump(src, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return cfg


def gift_event(payload: dict) -> dict:
    return {"type": "SEND_GIFT", "data": {"cmd": "SEND_GIFT", "data": payload}}


def nums(text: str) -> list[int]:
    out = []
    for m in re.finditer(r"-?\d+", text or ""):
        out.append(int(m.group()))
    return out


def main() -> int:
    from app.bot import LiveRobot
    from app.config import load_config
    from app.storage import StatsStore

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="kwstats_"))
    try:
        (tmp / "data").mkdir(parents=True, exist_ok=True)
        cfg_file = build_config(tmp)
        cfg_path_text = cfg_file.read_text(encoding="utf-8")
        cfg = load_config(str(cfg_file))
        store = StatsStore(cfg.storage.sqlite_path)

        replies: list[str] = []

        class _FakeAuth:
            status = "ok"

            def write_status(self, *a, **k) -> None:
                pass

            def mark_needs_login(self, *a, **k) -> None:
                pass

        robot = LiveRobot(config=cfg, credential=object(), auth=_FakeAuth())

        async def fake_enqueue(text: str, reply_uid=None, **kw) -> None:
            replies.append(text)

        robot._enqueue_reply = fake_enqueue  # type: ignore[assignment]

        # ── 一颗真实形状的盲盒：心动盲盒 ×2，单价 15000 金瓜子（=150 电池），
        #    开出 3000 金瓜子（=30 电池）→ 花费 300、收益 -240
        blind_payload = {
            "uid": UID,
            "uname": UNAME,
            "giftName": "心动盲盒",
            "num": 2,
            "tid": "kwstats-tid-1",
            "timestamp": 1790000000,
            "total_coin": 30000,
            "blind_gift": {"original_gift_price": 15000, "gift_tip_price": 3000},
        }
        # 一颗普通礼物：辣条 ×3（100 金瓜子 = 1 电池/件）
        normal_payload = {
            "uid": UID,
            "uname": UNAME,
            "giftName": "辣条",
            "num": 3,
            "tid": "kwstats-tid-2",
            "timestamp": 1790000001,
            "total_coin": 300,
            "coin_type": "gold",
        }

        async def feed() -> None:
            await robot._on_gift(gift_event(blind_payload))
            await robot._on_gift(gift_event(normal_payload))

        asyncio.run(feed())

        print("[1] 礼物真的进了库")
        rows = store._conn.execute(
            "SELECT gift_name, gift_num, is_blind_box, blind_box_cost, actual_value, "
            "profit_value FROM gift_events ORDER BY id").fetchall()
        ok("gift_events 有 2 条", len(rows) == 2, len(rows))
        blind_rows = [r for r in rows if r[2] == 1]
        ok("其中 1 条是盲盒", len(blind_rows) == 1, len(blind_rows))
        if blind_rows:
            r = blind_rows[0]
            ok("盲盒数量 = 2", r[1] == 2, r[1])
            ok("盲盒花费 = 300 电池", r[3] == 300, r[3])
            ok("开出价值 = 60 电池", r[4] == 60, r[4])
            ok("盈亏 = -240", r[5] == -240, r[5])

        print("[2] 聚合表跟着涨（关键词就是读这张表）")
        agg = store._conn.execute(
            "SELECT blind_box_count, cost_total, actual_total, profit_total "
            "FROM monthly_blindbox_stats WHERE uid=?", (UID,)).fetchone()
        ok("monthly_blindbox_stats 有这一行", agg is not None)
        if agg:
            ok("聚合：数量 2 / 花费 300 / 价值 60 / 盈亏 -240",
               tuple(agg) == (2, 300, 60, -240), tuple(agg))

        print("[3] 关键词回复不是 0")
        async def ask(text: str) -> str:
            replies.clear()
            await robot._on_danmaku({"type": "DANMU_MSG", "data": {
                "cmd": "DANMU_MSG",
                "info": [[0, 1, 25, 16777215, 1790000002, 0, 0, "", 0, 0, 0, "", 0, "{}", "{}", "{}", 0],
                         text, [UID, UNAME], [], [], 0, 0, 0, 0, 0, 999],
            }})
            return replies[-1] if replies else ""

        reply_month = asyncio.run(ask("#盲盒统计"))
        ok("「#盲盒统计」有回应", bool(reply_month), repr(reply_month)[:80])
        if reply_month:
            vals = nums(reply_month)
            ok("回复里出现花费 300（不是 0）", 300 in vals, reply_month)
            ok("回复里出现数量 2", 2 in vals, reply_month)
            ok("回复不是「本月盲盒共0个，花费0，收益0」", vals.count(0) == 0, reply_month)

        print("[4] 三个口径互相对得上")
        total_events = store._conn.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
        total_agg = store._conn.execute(
            "SELECT COUNT(*) FROM monthly_gift_stats").fetchone()[0]
        ok("gift_events 2 条 ↔ monthly_gift_stats 1 行（按用户聚合）",
           (total_events, total_agg) == (2, 1), f"{total_events}/{total_agg}")
        m = store._conn.execute(
            "SELECT gift_num_total, value_total FROM monthly_gift_stats WHERE uid=?",
            (UID,)).fetchone()
        # 口径说明：value_total = SUM(actual_value)，而非盲盒行的 actual_value 恒为 0
        # （普通礼物的价值只在 raw_json.total_coin 里，由排行 SQL 现算）。
        # 所以这里只可能是盲盒开出的 60。这是**既有口径**，不是 bug。
        ok("礼物聚合：件数 5（辣条3 + 盲盒2）", m and int(m[0]) == 5, tuple(m) if m else None)
        ok("value_total 只含盲盒开出价值 60（口径如此）", m and int(m[1]) == 60, tuple(m) if m else None)

        print("[5] 排行接口（用户看到的「价值」列）不能被普通礼物算成 0")
        import app.web.server as srv
        room_id = "1946287911"
        room_dir = tmp / "rooms" / room_id
        (room_dir / "data").mkdir(parents=True, exist_ok=True)
        (room_dir / "config.yaml").write_text(cfg_path_text, encoding="utf-8")
        old_base = getattr(srv, "_ROOMS_BASE_DIR", None)
        srv._ROOMS_BASE_DIR = str(tmp)
        try:
            srv._ROOM_DB_PATH_CACHE.clear()
            ranking = asyncio.run(srv.api_room_ranking(room_id))
        finally:
            srv._ROOMS_BASE_DIR = old_base
            srv._ROOM_DB_PATH_CACHE.clear()
        rows_r = (ranking or {}).get("ranking") or []
        ok("排行里有这个用户", any(int(r["uid"]) == UID for r in rows_r), rows_r[:1])
        mine = next((r for r in rows_r if int(r["uid"]) == UID), None)
        if mine:
            ok("条数 = 2（两件礼物）", int(mine["count"]) == 2, mine["count"])
            # 辣条 300 金瓜子 = 0.3 元（100 金瓜子=1电池，10 电池=1元）
            # 盲盒 60 电池 = 6 元；合计 6.3 元
            ok("价值不是 0（普通礼物按 total_coin 折算）",
               float(mine["total"]) > 0, mine["total"])
            ok("价值 = 6.3 元（盲盒 6 + 辣条 0.3）",
               abs(float(mine["total"]) - 6.3) < 0.02, mine["total"])
            ok("盈亏 = -24 元（盲盒 -240 电池）",
               abs(float(mine["total_profit"]) + 24.0) < 0.02, mine["total_profit"])

        print("[6] cmd 带后缀（SEND_GIFT:4:0:...）也必须能记上")
        before_n = store._conn.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
        suffixed = {
            "type": "SEND_GIFT:4:0:2:2:2:0",
            "data": {"cmd": "SEND_GIFT:4:0:2:2:2:0", "data": {
                "uid": UID, "uname": UNAME, "giftName": "小花花", "num": 1,
                "tid": "kwstats-tid-3", "timestamp": 1790000003, "total_coin": 100,
            }},
        }
        asyncio.run(robot._on_all_events(suffixed))
        after_n = store._conn.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
        ok("带后缀的礼物事件被记进库了", after_n == before_n + 1, f"{before_n} -> {after_n}")
        last = store._conn.execute(
            "SELECT gift_name, gift_num FROM gift_events ORDER BY id DESC LIMIT 1").fetchone()
        ok("记的是这件礼物（小花花 x1）", tuple(last) == ("小花花", 1), tuple(last))

        # 精确同名（无后缀）的事件由库的精确分发处理，ALL 里不能再记一次
        asyncio.run(robot._on_all_events({
            "type": "SEND_GIFT", "data": {"cmd": "SEND_GIFT", "data": {
                "uid": UID, "uname": UNAME, "giftName": "小花花", "num": 1,
                "tid": "kwstats-tid-4", "timestamp": 1790000004, "total_coin": 100}}}))
        same_n = store._conn.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
        ok("精确同名的事件不会被重复处理", same_n == after_n, f"{after_n} -> {same_n}")

        print("[7] 带后缀的 COMBO_SEND 不能被当成真礼物（那是连击汇总）")
        n_before = store._conn.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
        asyncio.run(robot._on_all_events({
            "type": "COMBO_SEND:1:0:0", "data": {"cmd": "COMBO_SEND:1:0:0", "data": {
                "uid": UID, "uname": UNAME, "combo_num": 10, "combo_total_coin": 2000,
                "gift_num": 0}}}))
        n_after = store._conn.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
        ok("连击汇总没有入库", n_after == n_before, f"{n_before} -> {n_after}")

        print("[8] 带后缀的礼物事件不能被报成「不认识的礼物事件」")
        robot._unknown_gift_like.clear()
        robot._event_counts.clear()
        asyncio.run(robot._on_all_events({
            "type": "SEND_GIFT:4:0:2:2:2:0", "data": {"cmd": "SEND_GIFT:4:0:2:2:2:0",
                "data": {"uid": UID, "uname": UNAME, "giftName": "小花花", "num": 1,
                         "tid": "kwstats-tid-5", "timestamp": 1790000005, "total_coin": 100}}}))
        ok("没有把它记成未知事件", not robot._unknown_gift_like,
           sorted(robot._unknown_gift_like))
        ok("计数归到基础名 SEND_GIFT 上", robot._event_counts.get("SEND_GIFT") == 1,
           dict(robot._event_counts))

        report = store.consistency_report() if hasattr(store, "consistency_report") else None
        if report is not None:
            bad = [r for r in (report.get("checks") or []) if not r.get("ok")]
            ok("consistency_report 全部 ok", not bad, bad[:2])

        print("[9] 聚合表漂移不能影响关键词回复（统计以明细为准）")
        # 真实库里就有这种漂移（明细 33 盒/开出 4020，聚合却写着 2880/-1650）：
        # 旧版本漏写聚合、修数据的脚本只改明细、中断……都可能留下永久偏差。
        # 以前「#盲盒统计」读 monthly_blindbox_stats，用户看到的就是那个偏小的数，
        # 而 WebUI 排行读明细 → 两个页面数字不一样，用户只会说「统计坏了」。
        store._conn.execute(
            "UPDATE monthly_blindbox_stats SET blind_box_count=0, cost_total=0,"
            " actual_total=0, profit_total=0 WHERE uid=?", (UID,))
        store._conn.commit()
        drift_reply = asyncio.run(ask("#盲盒统计"))
        dv = nums(drift_reply)
        ok("聚合被清零后，回复仍按明细给出花费 300", 300 in dv, drift_reply)
        ok("回复里数量仍是 2", 2 in dv, drift_reply)
        ok("回复里收益仍是 -240", -240 in dv, drift_reply)
        # 只有明细里真的没有盲盒时，才允许说「没有」
        store._conn.execute("UPDATE gift_events SET is_blind_box=0 WHERE uid=?", (UID,))
        store._conn.commit()
        none_reply = asyncio.run(ask("#盲盒统计"))
        ok("明细里没有盲盒时才回「无盲盒记录」", "盲盒" in none_reply and 300 not in nums(none_reply),
           none_reply)

        store.close() if hasattr(store, "close") else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("KEYWORD STATS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
