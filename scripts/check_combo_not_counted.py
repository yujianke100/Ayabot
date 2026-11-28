"""老库里的连击汇总行（COMBO_SEND）不能混进任何统计口径。

`gift_events.event_type` 历史上存的是原始协议名。`COMBO_SEND` 是**连击汇总**，
它是前面若干条 `SEND_GIFT` 的合计副本（金额与逐条之和完全相等，已用真实数据核对），
所以所有聚合/排行/导出一律只看 `SEND_GIFT`。

房间排行（`/api/rooms/{id}/ranking`）、导出、用户礼物列表早就这么做了，
但 `/api/external/*`（overlay / 第三方面板）和「精美导出」漏了 —— 于是：

- `total_events` / `total_gifts` 比实际多（每个连击多算一条）；
- 连击行的 `total_coin` 是 0（它只有 `combo_total_coin`），按礼物名分组时
  会多出一行「某礼物 ×0、0 元」；
- 盲盒统计同理。

实测真实库 `rooms/1946287911` 就有 19 条 COMBO_SEND / 354 条 SEND_GIFT，
也就是说 overlay 上的总数会比 WebUI 多 19。

    ./.venv/Scripts/python.exe scripts/check_combo_not_counted.py
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

UID = 66001


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
    tmp = Path(tempfile.mkdtemp(prefix="combo_"))
    try:
        db = tmp / "bot.db"
        st = StatsStore(str(db))
        now = int(time.time())

        def gift(name: str, num: int, coin: int, blind: bool = False,
                 cost: int = 0, actual: int = 0) -> None:
            st.record_gift_event(GiftEvent(
                ts=now, month=tzutil.month_str(now), uid=UID, uname="甲",
                event_type="SEND_GIFT", gift_name=name, gift_num=num,
                is_blind_box=blind, blind_box_cost=cost, actual_value=actual,
                profit_value=actual - cost,
                raw_json=json.dumps({"total_coin": coin, "giftName": name, "num": num})))

        gift("辣条", 1, 100)
        gift("辣条", 2, 200)
        gift("心动盲盒", 1, 15000, blind=True, cost=150, actual=30)
        # 老版本留下的连击汇总行：数量是上面几条的合计，金额字段不同（combo_total_coin）
        for _ in range(2):
            st.record_gift_event(GiftEvent(
                ts=now, month=tzutil.month_str(now), uid=UID, uname="甲",
                # 故意给非零 num：真实库里这 19 行是 0，但老版本可能写过数量，
                # 只有真的过滤了才不会被算进去（否则这条断言测不到任何东西）
                event_type="COMBO_SEND", gift_name="辣条", gift_num=5,
                is_blind_box=0, blind_box_cost=0, actual_value=0, profit_value=0,
                raw_json=json.dumps({"combo_total_coin": 2000, "combo_num": 3})))
        # 不能只看「函数没报错」：先确认夹具里确实有连击行
        raw_count = st._conn.execute(
            "SELECT COUNT(1) FROM gift_events WHERE event_type = 'COMBO_SEND'").fetchone()[0]
        st.close()
        ok("夹具里确实有 2 条 COMBO_SEND（否则下面的断言测不到东西）", raw_count == 2, raw_count)

        print("[1] 外部用户统计只数 SEND_GIFT")
        us = srv._external_user_stats_blocking(db, UID, "all")
        gift_part = us.get("gift") or {}
        ok("total_events = 3（不是 5）", gift_part.get("total_events") == 3,
           gift_part.get("total_events"))
        ok("total_gift_count = 4（1+2+1，连击行 gift_num=0 不该再增加）",
           gift_part.get("total_gift_count") == 4, gift_part.get("total_gift_count"))
        details = ((us.get("gift") or {}).get("details") or [])
        late_rows = [r for r in details if r.get("name") == "辣条"]
        ok("按礼物名分组里只有一行辣条", len(late_rows) == 1, details)
        if late_rows:
            ok("辣条数量 3（连击行不另算）", late_rows[0].get("count") == 3, late_rows[0])

        print("[2] 外部排行不把连击算进条数")
        rk = srv._external_ranking_blocking(db, "all", "all", 10)
        item = next((x for x in (rk.get("ranking") or []) if int(x.get("uid", 0)) == UID), None)
        ok("排行里能找到用户", item is not None, rk.get("ranking"))
        if item:
                ok("count = 4（1+2+1；如果连击行漏进来会是 14）", item.get("count") == 4,
               item.get("count"))

        print("[3] 外部房间总览")
        rs = srv._external_room_stats_blocking(db, "all")
        ov = rs.get("overview") or {}
        ok("total_events = 3", ov.get("total_events") == 3, ov.get("total_events"))
        ok("total_gifts = 4", ov.get("total_gifts") == 4, ov.get("total_gifts"))

        print("[4] 精美导出不导出连击副本")
        rows, _total = srv._export_all_blocking(db, "", "", "all", "", 0, 3000)
        late = [r for r in rows if r.get("gift_name") == "辣条"]
        total_num = sum(int(r.get("gift_num") or 0) for r in late)
        ok("辣条只出现一行（连击行被合并/排除）", len(late) == 1,
           [r.get("gift_name") for r in rows])
        ok("辣条数量合计 3", total_num == 3, total_num)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("COMBO NOT COUNTED OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
