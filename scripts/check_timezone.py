"""时区全局口径自检。

背景：这个项目原本同时存在三套时间算法 ——
  A. bot 写入用 `config.runtime.timezone`（`_now()`）
  B. storage / server 里的 `datetime.now()`、`datetime.fromtimestamp()`
  C. SQL 里的 `date(ts,'unixepoch','localtime')`（SQLite 的 localtime = 系统时区），
     还有一处 `date(ts,'unixepoch')` 干脆是 UTC
三者在「用户机器时区 ≠ 配置时区」时会互相打架：按月统计对不上按日期筛选，
删「某日之前」会多删/少删 8 小时，签到连续天数误判。

本脚本锁住修好之后的口径：**一律用 config.runtime.timezone**。

用法: ./.venv/Scripts/python.exe scripts/check_timezone.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SH = timezone(timedelta(hours=8))
UTC = timezone.utc

# 2024-12-31 17:00 UTC == 2025-01-01 01:00 +08 —— 跨年跨月跨天
EDGE_TS = 1735664400

fails: list[str] = []


def ok(label: str, cond: bool, extra=None) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    from app import tzutil
    from app.storage import GiftEvent, StatsStore

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_tz_"))

    try:
        # ── 1. tzutil 基本口径 ──
        print("[1] tzutil 基本口径")
        tzutil.set_config_timezone("Asia/Shanghai")
        ok("月按配置时区算", tzutil.month_str(EDGE_TS) == "2025-01", tzutil.month_str(EDGE_TS))
        ok("日按配置时区算", tzutil.day_str(EDGE_TS) == "2025-01-01", tzutil.day_str(EDGE_TS))
        tzutil.set_config_timezone("UTC")
        ok("切到 UTC 后月变了", tzutil.month_str(EDGE_TS) == "2024-12", tzutil.month_str(EDGE_TS))
        ok("切到 UTC 后日变了", tzutil.day_str(EDGE_TS) == "2024-12-31", tzutil.day_str(EDGE_TS))
        tzutil.set_config_timezone("Asia/Shanghai")

        # ── 2. 日区间 / 「某日之前」的截止点 ──
        print("[2] 日期区间换算")
        s_ts, e_ts = tzutil.day_range("2025-01-01")
        ok("当天起止 = 24 小时", e_ts - s_ts == 86399, e_ts - s_ts)
        ok("起点是配置时区的零点",
           tzutil.day_str(s_ts) == "2025-01-01" and tzutil.day_str(s_ts - 1) == "2024-12-31",
           [tzutil.day_str(s_ts), tzutil.day_str(s_ts - 1)])
        ok("range_before 等于当天零点", tzutil.range_before("2025-01-01") == s_ts)
        ok("跨月边界落点正确", tzutil.day_str(e_ts) == "2025-01-01")

        # ── 3. SQL 的 ayabot_date / ayabot_month（必须跟 Python 侧一致）──
        print("[3] SQL 函数与 Python 一致")
        db = tmp / "sql.db"
        st = StatsStore(str(db))
        got_d = st._conn.execute("SELECT ayabot_date(?)", (EDGE_TS,)).fetchone()[0]
        got_m = st._conn.execute("SELECT ayabot_month(?)", (EDGE_TS,)).fetchone()[0]
        ok("SQL ayabot_date 一致", got_d == tzutil.day_str(EDGE_TS), [got_d, tzutil.day_str(EDGE_TS)])
        ok("SQL ayabot_month 一致", got_m == tzutil.month_str(EDGE_TS), [got_m, tzutil.month_str(EDGE_TS)])
        ok("空时间戳返回 NULL",
           st._conn.execute("SELECT ayabot_date(NULL)").fetchone()[0] is None)

        # ── 4. 写入的 month 与按日期分组一致 ──
        print("[4] 写入的 month 与按日期分组一致")
        st.record_gift_event(GiftEvent(
            ts=EDGE_TS, month=tzutil.month_str(EDGE_TS), uid=1, uname="a",
            event_type="SEND_GIFT", gift_name="g", gift_num=1, is_blind_box=0,
            blind_box_cost=0, actual_value=0, profit_value=0, raw_json='{"total_coin":100}'))
        row = st._conn.execute(
            "SELECT month, ayabot_month(ts) FROM gift_events").fetchone()
        ok("month 列与 SQL 分组一致", row[0] == row[1] == "2025-01", row)
        ok("StatsStore.month_of_ts 一致", StatsStore.month_of_ts(EDGE_TS) == "2025-01",
           StatsStore.month_of_ts(EDGE_TS))

        # ── 5. 所有连接都注册了函数（漏了会直接 no such function）──
        print("[5] 连接注册检查")
        from app.web import server as srv
        conn = srv._connect(db)
        try:
            ok("server._connect 可用 ayabot_date",
               conn.execute("SELECT ayabot_date(?)", (EDGE_TS,)).fetchone()[0] == "2025-01-01")
        finally:
            conn.close()

        # ── 6. 按日期删除后聚合要按明细重算 ──
        print("[6] 按日期删除后聚合重算")
        db2 = tmp / "agg.db"
        st2 = StatsStore(str(db2))
        base_ts = tzutil.day_range("2025-01-05")[0]
        for i in range(6):
            st2.record_gift_event(GiftEvent(
                ts=base_ts + i * 3600, month="2025-01", uid=1, uname="a",
                event_type="SEND_GIFT", gift_name="g", gift_num=2, is_blind_box=0,
                blind_box_cost=0, actual_value=0, profit_value=0,
                raw_json='{"total_coin":100}'))
        before = st2._conn.execute(
            "SELECT gift_count, gift_num_total FROM monthly_gift_stats").fetchone()
        ok("累加了 6 次 / 12 件", tuple(before) == (6, 12), before)
        # 只删掉前 3 天（模拟 delete_old 的局部删除）
        cut = base_ts + 3 * 3600
        st2._conn.execute("DELETE FROM gift_events WHERE ts < ?", (cut,))
        st2._conn.commit()
        st2.rebuild_monthly_stats(["2025-01"])
        after = st2._conn.execute(
            "SELECT gift_count, gift_num_total FROM monthly_gift_stats").fetchone()
        ok("重算后与明细一致（3 次 / 6 件）", tuple(after) == (3, 6), after)

        # ── 7. 签到日期与 bot 写入的场次日期同口径 ──
        print("[7] 签到与开播日期同口径")
        st2.record_stream_date(tzutil.today_str())
        prev = st2._get_previous_stream_date(
            (datetime.now(tzutil.get_tz()) + timedelta(days=1)).strftime("%Y-%m-%d"))
        ok("能查到今天这场（说明写入/读取同一口径）",
           prev == tzutil.today_str(), [prev, tzutil.today_str()])


        # ── 9. 重算不能抹掉「只有聚合、明细已清」的月份 ──
        print("[9] 重算的边界：聚合还在但明细没了")
        db3 = tmp / "keep.db"
        st3 = StatsStore(str(db3))
        jan_ts = tzutil.day_range("2025-01-10")[0]
        st3.record_gift_event(GiftEvent(
            ts=jan_ts, month="2025-01", uid=1, uname="a", event_type="SEND_GIFT",
            gift_name="g", gift_num=1, is_blind_box=0, blind_box_cost=0,
            actual_value=0, profit_value=0, raw_json='{"total_coin":100}'))
        # 手工塞一个「明细已过期、只剩聚合」的月份
        st3._conn.execute(
            "INSERT INTO monthly_gift_stats (month, uid, uname, gift_count, gift_num_total,"
            " value_total, last_ts) VALUES ('2024-05', 9, 'old', 7, 7, 0, 0)")
        st3._conn.commit()
        st3.rebuild_monthly_stats()
        kept = st3._conn.execute(
            "SELECT gift_count FROM monthly_gift_stats WHERE month='2024-05'").fetchone()
        ok("全量重算保留了无明细月份", kept and kept[0] == 7, kept)
        redo = st3._conn.execute(
            "SELECT gift_count FROM monthly_gift_stats WHERE month='2025-01'").fetchone()
        ok("有明细的月份被正确重算", redo and redo[0] == 1, redo)

        # ── 10. 按日期删除后聚合要收缩（不是只增不减）──
        print("[10] 部分删除后聚合收缩")
        st3.record_gift_event(GiftEvent(
            ts=jan_ts + 3600, month="2025-01", uid=1, uname="a", event_type="SEND_GIFT",
            gift_name="g", gift_num=1, is_blind_box=0, blind_box_cost=0,
            actual_value=0, profit_value=0, raw_json='{"total_coin":100}'))
        before = st3._conn.execute(
            "SELECT gift_count FROM monthly_gift_stats WHERE month='2025-01'").fetchone()[0]
        st3._conn.execute("DELETE FROM gift_events WHERE ts < ?", (jan_ts + 1,))
        st3._conn.commit()
        st3.rebuild_monthly_stats(["2025-01"])
        after = st3._conn.execute(
            "SELECT gift_count FROM monthly_gift_stats WHERE month='2025-01'").fetchone()[0]
        ok("聚合从 2 收缩到 1", (before, after) == (2, 1), [before, after])
        st3._conn.close()

        # ── 11. 日期过滤必须走 ts 区间（性能 + 口径）──
        print("[11] 日期过滤不逐行回调")
        src_storage = (ROOT / "app" / "storage.py").read_text(encoding="utf-8")
        src_server = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        # 只禁「拿 ayabot_date 做比较过滤」这一种写法。
        # SELECT DISTINCT ayabot_date(ts) 是列日期列表，属于正常用法，不算性能问题。
        import re as _re
        pat = _re.compile(r"ayabot_date\(ts\)\s*(>=|<=|>|<|=)")
        for name, src in (("storage.py", src_storage), ("server.py", src_server)):
            # 只算真正的 SQL 字符串行：docstring / 注释里提到这种写法不算违规
            bad = [ln.strip() for ln in src.splitlines()
                   if pat.search(ln) and ('"' in ln or "'" in ln)
                   and not ln.strip().startswith(("#", "//"))]
            ok(f"{name} 不用 ayabot_date(ts) 做范围过滤", not bad, bad[:2])
        ok("日期区间统一走 _day_bounds/_date_bounds",
           "_day_bounds(" in src_server and "_date_bounds(" in src_storage)

        # ── 12. 日期过滤的结果与按天分组一致 ──
        print("[12] 区间过滤 ≡ 按天分组")
        db4 = tmp / "range.db"
        st4 = StatsStore(str(db4))
        d1 = tzutil.day_range("2025-03-01")[0]
        for off in (0, 100, 86399, 86400, 90000):   # 跨天边界
            st4.record_gift_event(GiftEvent(
                ts=d1 + off, month="2025-03", uid=off + 1, uname="u",
                event_type="SEND_GIFT", gift_name="g", gift_num=1, is_blind_box=0,
                blind_box_cost=0, actual_value=0, profit_value=0,
                raw_json='{"total_coin":100}'))
        lo, hi = tzutil.day_range("2025-03-01")
        by_range = st4._conn.execute(
            "SELECT COUNT(1) FROM gift_events WHERE ts >= ? AND ts <= ?", (lo, hi)).fetchone()[0]
        by_day = st4._conn.execute(
            "SELECT COUNT(1) FROM gift_events WHERE ayabot_date(ts) = '2025-03-01'").fetchone()[0]
        ok("区间与分组结果一致（3 条）", by_range == by_day == 3, [by_range, by_day])
        st4._conn.close()


        # ── 13. 时区数据必须真的可用（Windows 没有系统 tzdb，全靠 tzdata 包）──
        print("[13] tzdata 依赖")
        try:
            import tzdata  # noqa: F401
            has_tzdata = True
        except ImportError:
            has_tzdata = False
        ok("tzdata 包可用（ZoneInfo 依赖它）", has_tzdata)
        tzutil.set_config_timezone("Asia/Shanghai")
        ok("ZoneInfo 能解析 Asia/Shanghai",
           tzutil.get_tz() is not None, tzutil.get_tz())
        tzutil.set_config_timezone("Europe/Berlin")
        de = tzutil.get_tz()
        ok("换一个时区也能解析（不是写死东八区）", de is not None, de)
        # 柏林夏令时：7 月 UTC+2，1 月 UTC+1
        ok("夏令时换算正确",
           tzutil.month_str(1721000000) == "2024-07"
           and tzutil.day_str(1721000000) == "2024-07-15", tzutil.day_str(1721000000))
        tzutil.set_config_timezone("Asia/Shanghai")

        # ── 14. 构建脚本要把 tzdata 数据一起打进去 ──
        print("[14] 构建脚本")
        build_src = (ROOT / "scripts" / "build_exe.py").read_text(encoding="utf-8")
        ok("打包脚本 collect tzdata 数据", '"--collect-data", "tzdata"' in build_src)
        req_src = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        ok("requirements 显式声明 tzdata", "tzdata" in req_src)

        # ── 8. 源码里不许再出现裸的 localtime / 系统时区 ──
        print("[8] 源码禁用项")
        for f in ("app/storage.py", "app/web/server.py", "app/bot.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            ok(f"{f} 无本地时区 localtime", "'localtime'" not in src)
        for f in ("app/storage.py", "app/web/server.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            bad = [ln.strip() for ln in src.splitlines()
                   if ("datetime.now()" in ln or "datetime.datetime.now()" in ln)
                   and not ln.strip().startswith("#")]
            ok(f"{f} 无 datetime.now() 直接取当前时间", not bad, bad[:3])
        src_bot = (ROOT / "app" / "bot.py").read_text(encoding="utf-8")
        ok("bot 记录礼物/上舰用配置时区 _now()",
           'month = _now().strftime("%Y-%m")' in src_bot
           and 'month=_now().strftime("%Y-%m")' in src_bot)
        ok("bot 时段规则用配置时区",
           "h = _now().hour" in src_bot and "now_h = _now().hour" in src_bot)
        src_srv = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        # 注释里会提到这句话，所以只看真正的代码行（Python 的 docstring 也排除）
        iso_lines = [ln.strip() for ln in src_srv.splitlines()
                     if "new Date().toISOString()" in ln
                     and not ln.strip().startswith(("#", "//", '*', '"""', "前端不能"))
                     and "前端不能拿" not in ln]
        ok("前端不再用 toISOString 取今天", not iso_lines, iso_lines[:3])
        ok("有 /api/time 供前端校准今天", '"/api/time"' in src_srv)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(" -", f)
        return 1
    print("\nTIMEZONE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
