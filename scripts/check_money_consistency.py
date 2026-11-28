"""三个「给用户看的金额」必须互相对得上，而且单位不能串。

同一份数据在界面上有三个出口，任何一个算错，用户看到的就是「统计不对」：

  1. `/api/external/ranking`（第三方面板 / overlay）→ `value_yuan`（元）
  2. 「精美导出」→ 每行 `actual_value`（元）
  3. 数据库明细（`gift_events` + `raw_json.total_coin`）→ 自己算一遍

金额口径（全项目唯一口径）：
  - 普通礼物：`raw_json.total_coin` 是金瓜子，÷100 = 电池，÷1000 = 元
  - 盲盒：`actual_value` / `blind_box_cost` / `profit_value` 本身就是**电池**，÷10 = 元
  - `gift_events.actual_value` 对普通礼物**恒为 0**，不是它的价值

    ./.venv/Scripts/python.exe scripts/check_money_consistency.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UID = 77001


def main() -> int:
    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    from app import tzutil
    from app.storage import GiftEvent, StatsStore
    from app.web import server as srv

    tzutil.set_config_timezone("Asia/Shanghai")
    tmp = Path(tempfile.mkdtemp(prefix="money_"))
    try:
        db = tmp / "bot.db"
        st = StatsStore(str(db))
        now = int(time.time())
        month = tzutil.month_str(now)

        def add(name, num, coin, blind=False, cost=0, actual=0, etype="SEND_GIFT"):
            st.record_gift_event(GiftEvent(
                ts=now, month=month, uid=UID, uname="甲", event_type=etype,
                gift_name=name, gift_num=num, is_blind_box=blind,
                blind_box_cost=cost, actual_value=actual,
                profit_value=actual - cost,
                raw_json=json.dumps({"total_coin": coin, "giftName": name, "num": num})))

        # 辣条 100 金瓜子 = 0.1 元
        add("辣条", 1, 100)
        # 情书 5200 金瓜子 = 5.2 元
        add("情书", 1, 5200)
        # 盲盒：花 150 电池（15 元），开出 30 电池（3 元）→ 盈亏 -12 元
        add("心动盲盒", 1, 15000, blind=True, cost=150, actual=30)
        # 连击汇总副本：绝不能改变任何金额
        add("辣条", 0, 0, etype="COMBO_SEND")
        st.close()

        expect_yuan = round((100 + 5200) / 1000 + 30 / 10, 4)   # 5.3 + 3.0 = 8.3
        expect_battery = (100 + 5200) // 100 + 30               # 53 + 30 = 83

        print("[1] 外部排行的元 / 电池两个字段")
        rk = srv._external_ranking_blocking(db, "all", "all", 10)
        item = next((x for x in rk.get("ranking") or [] if int(x.get("uid", 0)) == UID), None)
        ok("排行里有该用户", item is not None, rk.get("ranking"))
        if item:
            ok(f"value_yuan = {expect_yuan}", abs(float(item.get("value_yuan", -1)) - expect_yuan) < 1e-6,
               item.get("value_yuan"))
            ok(f"value = {expect_battery} 电池",
               abs(float(item.get("value", -1)) - expect_battery) < 1e-6, item.get("value"))
            # 盈亏只算盲盒：30 - 150 = -120 电池 = -12 元
            ok("盈亏按电池算（-120）", float(item.get("profit", 0)) == -120.0, item.get("profit"))
            ok("盈亏折算成元（-12.0）", float(item.get("profit_yuan", 0)) == -12.0,
               item.get("profit_yuan"))

        print("[2] 精美导出的行金额（元）")
        rows, total = srv._export_all_blocking(db, "", "", "all", "", 0, 3000)
        ok("导出行数 = 3（连击副本被排除）", len(rows) == 3,
           [(r.get("gift_name"), r.get("gift_num")) for r in rows])
        got = round(sum(float(r.get("actual_value") or 0) for r in rows), 4)
        ok(f"导出合计 = {expect_yuan} 元（和排行一致）", abs(got - expect_yuan) < 1e-6, got)
        ok("total_rows 头也是 3", int(total) == 3, total)

        print("[3] 明细自己算一遍（唯一事实来源）")
        import sqlite3
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = c.execute("""
            SELECT COALESCE(SUM(CASE WHEN is_blind_box = 0
                                     THEN CAST(json_extract(raw_json, '$.total_coin') AS INTEGER) / 100
                                     ELSE actual_value END), 0)
            FROM gift_events WHERE uid = ? AND event_type = 'SEND_GIFT'
        """, (UID,)).fetchone()
        c.close()
        ok(f"明细算出 {expect_battery} 电池", int(row[0]) == expect_battery, row[0])

        print("[4] 普通礼物的 actual_value 必须仍是 0（口径不是「价值」列）")
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n = c.execute("SELECT COALESCE(SUM(actual_value), 0) FROM gift_events"
                      " WHERE uid = ? AND is_blind_box = 0", (UID,)).fetchone()[0]
        c.close()
        ok("普通礼物合计 actual_value = 0", int(n) == 0, n)
        print("      （所以任何拿 SUM(actual_value) 当礼物总价值的 SQL 都会得到 0 —— "
              "遗留接口 /api/ranking 正是这个 bug，已修）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("MONEY CONSISTENCY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
