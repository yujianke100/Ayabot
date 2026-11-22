from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from app import tzutil


@dataclass(slots=True)
class GiftEvent:
    ts: int
    month: str
    uid: int
    uname: str
    event_type: str
    gift_name: str
    gift_num: int
    is_blind_box: int
    blind_box_cost: int
    actual_value: int
    profit_value: int
    raw_json: str


def _date_bounds(date_from: str = "", date_to: str = "") -> tuple[int | None, int | None]:
    """把 `YYYY-MM-DD` 的日期区间换成配置时区的 [起, 止] 时间戳（闭区间）。

    空串表示该侧不限。日期格式不对时抛 ValueError，让调用方按 400 处理。
    """
    lo = tzutil.day_range(date_from)[0] if date_from else None
    hi = tzutil.day_range(date_to)[1] if date_to else None
    return lo, hi


def _date_range_where(date_from: str = "", date_to: str = "") -> tuple[str, list]:
    """生成 ` WHERE ts >= ? AND ts <= ?` 片段 + 参数。

    之所以不写成 `ayabot_date(ts) >= ?`：SQLite 里调 Python 回调是逐行开销，
    20 万行要 0.6s（日期区间只走索引是 6ms）。日期口径照样是配置时区。
    """
    lo, hi = _date_bounds(date_from, date_to)
    clauses, params = [], []
    if lo is not None:
        clauses.append("ts >= ?")
        params.append(lo)
    if hi is not None:
        clauses.append("ts <= ?")
        params.append(hi)
    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), params


class StatsStore:
    def __init__(self, db_path: str) -> None:
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(p)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        # 房间机器人可能同时在写同一个库：等锁而不是直接报 database is locked
        self._conn.execute("PRAGMA busy_timeout=8000;")
        # SQL 里按天/按月分组要用配置时区，不能用 SQLite 的 localtime（那是系统时区）
        tzutil.install_sqlite_functions(self._conn)
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS gift_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                month TEXT NOT NULL,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL,
                event_type TEXT NOT NULL,
                gift_name TEXT NOT NULL,
                gift_num INTEGER NOT NULL,
                is_blind_box INTEGER NOT NULL,
                blind_box_cost INTEGER NOT NULL,
                actual_value INTEGER NOT NULL,
                profit_value INTEGER NOT NULL,
                raw_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_gift_events_month_uid
            ON gift_events(month, uid);

            CREATE INDEX IF NOT EXISTS idx_gift_events_month_blind
            ON gift_events(month, is_blind_box);

            CREATE INDEX IF NOT EXISTS idx_gift_events_ts
            ON gift_events(ts);

            CREATE TABLE IF NOT EXISTS monthly_blindbox_stats (
                month TEXT NOT NULL,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL,
                blind_box_count INTEGER NOT NULL,
                cost_total INTEGER NOT NULL,
                actual_total INTEGER NOT NULL,
                profit_total INTEGER NOT NULL,
                last_ts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(month, uid)
            );

            CREATE TABLE IF NOT EXISTS monthly_gift_stats (
                month TEXT NOT NULL,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL,
                gift_count INTEGER NOT NULL DEFAULT 0,
                gift_num_total INTEGER NOT NULL DEFAULT 0,
                value_total INTEGER NOT NULL DEFAULT 0,
                last_ts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(month, uid)
            );

            CREATE TABLE IF NOT EXISTS user_checkin (
                uid INTEGER PRIMARY KEY,
                uname TEXT NOT NULL,
                last_checkin_date TEXT NOT NULL,
                continuous_days INTEGER NOT NULL DEFAULT 1,
                total_days INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS anchor_stream_dates (
                date TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS danmaku_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL,
                content TEXT NOT NULL
            );

            -- 大航海（上舰）记录：B站 同一次开通会推送 GUARD_BUY / USER_TOAST_MSG /
            -- USER_TOAST_MSG_V2 三种事件，靠 (uid, start_time) 唯一索引去重
            CREATE TABLE IF NOT EXISTS guard_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                month TEXT NOT NULL,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL,
                guard_level INTEGER NOT NULL,
                guard_name TEXT NOT NULL DEFAULT '',
                months INTEGER NOT NULL DEFAULT 1,
                price_coin INTEGER NOT NULL DEFAULT 0,
                value INTEGER NOT NULL DEFAULT 0,
                start_time INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT ''
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_guard_events_dedupe
            ON guard_events(uid, start_time) WHERE start_time > 0;

            CREATE INDEX IF NOT EXISTS idx_guard_events_ts
            ON guard_events(ts);
            """
        )
        self._conn.commit()
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """老库补列：dedupe_key（导入去重）+ source（数据来源，便于撤销导入）。"""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(gift_events)")}
        added = False
        if "dedupe_key" not in cols:
            self._conn.execute(
                "ALTER TABLE gift_events ADD COLUMN dedupe_key TEXT NOT NULL DEFAULT ''")
            added = True
        if "source" not in cols:
            self._conn.execute(
                "ALTER TABLE gift_events ADD COLUMN source TEXT NOT NULL DEFAULT ''")
            added = True
        # 部分唯一索引：只对「带 key 的导入行」生效，实时行（key 为空）行为完全不变
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_gift_events_dedupe "
            "ON gift_events(dedupe_key) WHERE dedupe_key <> ''")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gift_events_source ON gift_events(source)")
        if added:
            self._conn.commit()

    @staticmethod
    def month_of_ts(ts: int) -> str:
        """时间戳 → YYYY-MM，按配置时区（与实时链路写入的 month 同口径）。"""
        return tzutil.month_str(ts)

    def record_gift_event(self, event: GiftEvent, dedupe_key: str = "", source: str = "",
                          commit: bool = True) -> bool:
        """写入一条礼物事件。返回是否真的新增（重复导入会返回 False 且不动统计）。

        dedupe_key 为空 = 实时事件，不做去重（保持原有行为）。
        导入历史数据时传稳定的 dedupe_key，同一份 XML 重复导入不会重复计数。
        """
        cur = self._conn.execute(
            """
            INSERT INTO gift_events (
                ts, month, uid, uname, event_type, gift_name, gift_num,
                is_blind_box, blind_box_cost, actual_value, profit_value, raw_json,
                dedupe_key, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            -- 只忽略「同一个导入去重键」这一种冲突；不要用 INSERT OR IGNORE，
            -- 那会把 NOT NULL 之类的真错误一起静默吞掉（实时链路就查不出问题了）
            ON CONFLICT(dedupe_key) WHERE dedupe_key <> '' DO NOTHING
            """,
            (
                event.ts,
                event.month,
                event.uid,
                event.uname,
                event.event_type,
                event.gift_name,
                event.gift_num,
                event.is_blind_box,
                event.blind_box_cost,
                event.actual_value,
                event.profit_value,
                event.raw_json,
                dedupe_key,
                source,
            ),
        )
        if cur.rowcount == 0:
            # 命中 dedupe_key 唯一索引：重复导入，统计不动
            if commit:
                self._conn.commit()
            return False

        if event.is_blind_box:
            self._conn.execute(
                """
                INSERT INTO monthly_blindbox_stats (
                    month, uid, uname, blind_box_count,
                    cost_total, actual_total, profit_total, last_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(month, uid) DO UPDATE SET
                    uname=excluded.uname,
                    blind_box_count=monthly_blindbox_stats.blind_box_count + excluded.blind_box_count,
                    cost_total=monthly_blindbox_stats.cost_total + excluded.cost_total,
                    actual_total=monthly_blindbox_stats.actual_total + excluded.actual_total,
                    profit_total=monthly_blindbox_stats.profit_total + excluded.profit_total,
                    last_ts=MAX(monthly_blindbox_stats.last_ts, excluded.last_ts)
                """,
                (
                    event.month,
                    event.uid,
                    event.uname,
                    event.gift_num,
                    event.blind_box_cost,
                    event.actual_value,
                    event.profit_value,
                    event.ts,
                ),
            )

        # Also update monthly gift stats (both regular and blindbox gifts)
        self._conn.execute(
            """
            INSERT INTO monthly_gift_stats (
                month, uid, uname, gift_count, gift_num_total, value_total, last_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(month, uid) DO UPDATE SET
                uname=excluded.uname,
                gift_count=monthly_gift_stats.gift_count + 1,
                gift_num_total=monthly_gift_stats.gift_num_total + excluded.gift_num_total,
                value_total=monthly_gift_stats.value_total + excluded.value_total,
                last_ts=MAX(monthly_gift_stats.last_ts, excluded.last_ts)
            """,
            (
                event.month,
                event.uid,
                event.uname,
                1,
                event.gift_num,
                event.actual_value,
                event.ts,
            ),
        )

        if commit:
            self._conn.commit()
        return True

    def get_user_monthly_blindbox(self, month: str, uid: int) -> Optional[tuple[int, int, int, int]]:
        row = self._conn.execute(
            """
            SELECT blind_box_count, cost_total, actual_total, profit_total
            FROM monthly_blindbox_stats
            WHERE month = ? AND uid = ?
            """,
            (month, uid),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), int(row[1]), int(row[2]), int(row[3])

    def get_user_monthly_blindbox_by_uname(self, month: str, uname: str) -> Optional[tuple[int, int, int, int, int]]:
        row = self._conn.execute(
            """
            SELECT uid, blind_box_count, cost_total, actual_total, profit_total
            FROM monthly_blindbox_stats
            WHERE month = ? AND uname = ?
            LIMIT 1
            """,
            (month, uname),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4])

    def get_user_monthly_gift_activity(self, month: str, uid: int) -> tuple[int, int]:
        row = self._conn.execute(
            """
            SELECT COUNT(1), COALESCE(SUM(gift_num), 0)
            FROM gift_events
            WHERE month = ? AND uid = ?
            """,
            (month, uid),
        ).fetchone()
        if row is None:
            return 0, 0
        return int(row[0]), int(row[1])

    def get_monthly_blindbox_rank(self, month: str, limit: int = 10) -> list[tuple[str, int, int, int, int]]:
        rows = self._conn.execute(
            """
            SELECT uname, blind_box_count, cost_total, actual_total, profit_total
            FROM monthly_blindbox_stats
            WHERE month = ?
            ORDER BY profit_total DESC
            LIMIT ?
            """,
            (month, limit),
        ).fetchall()
        return [(str(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4])) for r in rows]

    def get_monthly_total_blindbox(self, month: str) -> tuple[int, int, int, int]:
        row = self._conn.execute(
            """
            SELECT SUM(blind_box_count), SUM(cost_total), SUM(actual_total), SUM(profit_total)
            FROM monthly_blindbox_stats
            WHERE month = ?
            """,
            (month,),
        ).fetchone()
        if not row or row[0] is None:
            return 0, 0, 0, 0
        return int(row[0]), int(row[1]), int(row[2]), int(row[3])

    def record_stream_date(self, date: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO anchor_stream_dates (date) VALUES (?)",
            (date,),
        )
        self._conn.commit()

    def _get_previous_stream_date(self, today: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT date FROM anchor_stream_dates WHERE date < ? ORDER BY date DESC LIMIT 1",
            (today,),
        ).fetchone()
        return row[0] if row else None

    def user_checkin(self, uid: int, uname: str) -> tuple[int, int, bool]:
        """ 返回 (连续签到天数, 连续天数排名, 今日是否已签到) """
        # 必须与 app/bot.py 里 record_stream_date(_now()) 同口径，否则跨天/时区不一致会误判断签
        today = tzutil.today_str()
        prev_stream_date = self._get_previous_stream_date(today)
        already_checked = False

        # 获取当前签到状态
        row = self._conn.execute(
            "SELECT last_checkin_date, continuous_days FROM user_checkin WHERE uid = ?", (uid,)
        ).fetchone()

        if row:
            last_date, continuous = row[0], row[1]
            if last_date == today:
                # 今天已经签到过了，直接返回当前数据
                already_checked = True
            elif prev_stream_date and last_date == prev_stream_date:
                # 上次签到是上一次开播日期，直播场场不落，续签
                continuous += 1
                self._conn.execute(
                    "UPDATE user_checkin SET last_checkin_date = ?, continuous_days = ?, total_days = total_days + 1, uname = ? WHERE uid = ?",
                    (today, continuous, uname, uid),
                )
            else:
                # 漏了某场直播（或不存在上一次开播记录）-> 断签
                continuous = 1
                self._conn.execute(
                    "UPDATE user_checkin SET last_checkin_date = ?, continuous_days = ?, total_days = total_days + 1, uname = ? WHERE uid = ?",
                    (today, continuous, uname, uid),
                )
        else:
            # 第一次签到
            continuous = 1
            self._conn.execute(
                "INSERT INTO user_checkin (uid, uname, last_checkin_date, continuous_days, total_days) VALUES (?, ?, ?, 1, 1)",
                (uid, uname, today),
            )
        
        self._conn.commit()

        # 计算排名
        rank_row = self._conn.execute(
            "SELECT COUNT(1) FROM user_checkin WHERE continuous_days > ?", (continuous,)
        ).fetchone()
        rank = (rank_row[0] if rank_row else 0) + 1

        return continuous, rank, already_checked

    def get_today_total_blindbox(
        self, start_ts: int, end_ts: int,
    ) -> tuple[int, int, int, int]:
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(gift_num), 0), COALESCE(SUM(blind_box_cost), 0),
                   COALESCE(SUM(actual_value), 0), COALESCE(SUM(profit_value), 0)
            FROM gift_events
            WHERE ts >= ? AND ts < ? AND is_blind_box = 1
            """,
            (start_ts, end_ts),
        ).fetchone()
        return int(row[0]), int(row[1]), int(row[2]), int(row[3])

    def get_today_user_blindbox(
        self, start_ts: int, end_ts: int, uid: int,
    ) -> Optional[tuple[int, int, int, int]]:
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(gift_num), 0), COALESCE(SUM(blind_box_cost), 0),
                   COALESCE(SUM(actual_value), 0), COALESCE(SUM(profit_value), 0)
            FROM gift_events
            WHERE ts >= ? AND ts < ? AND uid = ? AND is_blind_box = 1
            """,
            (start_ts, end_ts, uid),
        ).fetchone()
        if row and int(row[0]) > 0:
            return int(row[0]), int(row[1]), int(row[2]), int(row[3])
        return None

    def get_today_user_blindbox_by_uname(
        self, start_ts: int, end_ts: int, uname: str,
    ) -> Optional[tuple[int, int, int, int, int]]:
        rows = self._conn.execute(
            """
            SELECT uid, COALESCE(SUM(gift_num), 0), COALESCE(SUM(blind_box_cost), 0),
                   COALESCE(SUM(actual_value), 0), COALESCE(SUM(profit_value), 0)
            FROM gift_events
            WHERE ts >= ? AND ts < ? AND uname = ? AND is_blind_box = 1
            GROUP BY uid
            ORDER BY SUM(blind_box_cost) DESC
            LIMIT 1
            """,
            (start_ts, end_ts, uname),
        ).fetchone()
        if rows and int(rows[1]) > 0:
            return int(rows[0]), int(rows[1]), int(rows[2]), int(rows[3]), int(rows[4])
        return None

    def get_today_user_gift_activity(
        self, start_ts: int, end_ts: int, uid: int,
    ) -> tuple[int, int]:
        row = self._conn.execute(
            """
            SELECT COUNT(1), COALESCE(SUM(gift_num), 0)
            FROM gift_events
            WHERE ts >= ? AND ts < ? AND uid = ?
            """,
            (start_ts, end_ts, uid),
        ).fetchone()
        return int(row[0]), int(row[1])

    def record_danmaku(self, ts: int, uid: int, uname: str, content: str, max_entries: int = 1000) -> None:
        self._conn.execute(
            "INSERT INTO danmaku_log (ts, uid, uname, content) VALUES (?, ?, ?, ?)",
            (ts, uid, uname, content),
        )
        if max_entries > 0:
            self._conn.execute(
                "DELETE FROM danmaku_log WHERE id <= (SELECT id FROM danmaku_log ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (max_entries,),
            )
        self._conn.commit()

    def get_danmaku_dates(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT ayabot_date(ts) AS d FROM danmaku_log ORDER BY d"
        ).fetchall()
        return [r[0] for r in rows if r[0]]

    def get_danmaku_log(self, limit: int = 50, offset: int = 0, date_from: str = "", date_to: str = "", asc: bool = True) -> list[dict[str, int | str]]:
        # 日期过滤一律换算成 ts 区间：既按配置时区算，又能用上 ts 索引。
        # 不要在 SQL 里写 ayabot_date(ts) —— 那是逐行回调 Python，20 万行要 0.6s，而区间比较是 6ms。
        where, params = _date_range_where(date_from, date_to)
        order_dir = "ASC" if asc else "DESC"
        sql = f"SELECT id, ts, uid, uname, content FROM danmaku_log{where} ORDER BY id {order_dir} LIMIT ? OFFSET ?"
        rows = self._conn.execute(sql, [*params, limit, offset]).fetchall()
        return [
            {"id": int(r[0]), "ts": int(r[1]), "uid": int(r[2]), "uname": str(r[3]), "content": str(r[4])}
            for r in rows
        ]

    def get_danmaku_log_count(self, date_from: str = "", date_to: str = "") -> int:
        where, params = _date_range_where(date_from, date_to)
        row = self._conn.execute(f"SELECT COUNT(*) FROM danmaku_log{where}", params).fetchone()
        return int(row[0]) if row else 0

    def clear_danmaku_log(self, date_from: str = "", date_to: str = "") -> int:
        if date_from or date_to:
            where, params = _date_range_where(date_from, date_to)
            count = self._conn.execute(f"DELETE FROM danmaku_log{where}", params).rowcount
        else:
            count = self._conn.execute("DELETE FROM danmaku_log").rowcount
        self._conn.commit()
        return count

    def cleanup_old_data(self, gift_days: int = 733, danmaku_days: int = 0) -> dict[str, int]:
        """删除超过指定天数的旧数据. 天数=0 表示不清理."""
        now_ts = int(time.time())
        result: dict[str, int] = {}
        if gift_days > 0:
            cutoff = now_ts - gift_days * 86400
            # 先记下会被删到哪些月份，删完才能只重算它们
            affected_months = {r[0] for r in self._conn.execute(
                "SELECT DISTINCT month FROM gift_events WHERE ts < ?", (cutoff,))}
            deleted = self._conn.execute(
                "DELETE FROM gift_events WHERE ts < ?", (cutoff,)
            ).rowcount
            result["deleted_gifts"] = deleted
            deleted_guards = self._conn.execute(
                "DELETE FROM guard_events WHERE ts < ?", (cutoff,)
            ).rowcount
            result["deleted_guards"] = deleted_guards
            if deleted:
                # 边界月份只删了一部分明细，聚合是增量累加的，不重算就会一直比明细大。
                # 受影响月份必须在删除前就查好（删完就查不到了），见上面的 affected_months。
                self.rebuild_monthly_stats(sorted(affected_months))
                self._conn.execute(
                    "DELETE FROM monthly_blindbox_stats WHERE month NOT IN (SELECT DISTINCT month FROM gift_events)"
                )
                self._conn.execute(
                    "DELETE FROM monthly_gift_stats WHERE month NOT IN (SELECT DISTINCT month FROM gift_events)"
                )
        if danmaku_days > 0:
            cutoff = now_ts - danmaku_days * 86400
            deleted = self._conn.execute(
                "DELETE FROM danmaku_log WHERE ts < ?", (cutoff,)
            ).rowcount
            result["deleted_danmaku"] = deleted
        self._conn.commit()
        return result

    # ── 大航海（上舰） ──────────────────────────────────────

    def record_guard_event(
        self,
        *,
        ts: int,
        month: str,
        uid: int,
        uname: str,
        guard_level: int,
        guard_name: str = "",
        months: int = 1,
        price_coin: int = 0,
        value: int = 0,
        start_time: int = 0,
        source: str = "",
        raw_json: str = "",
        commit: bool = True,
    ) -> bool:
        """写入一条上舰记录（已存在则仅在价格不同时更新金额）。返回是否新增行。

        B站 同一次开通会推 GUARD_BUY（原价）与 USER_TOAST_MSG*（实付价），
        先到先存，后到的实付价会把金额纠正过来。
        """
        cur = self._conn.execute(
            """
            INSERT INTO guard_events (
                ts, month, uid, uname, guard_level, guard_name,
                months, price_coin, value, start_time, source, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid, start_time) WHERE start_time > 0 DO UPDATE SET
                price_coin = CASE
                    WHEN excluded.price_coin > 0 AND excluded.price_coin <> guard_events.price_coin
                    THEN excluded.price_coin ELSE guard_events.price_coin END,
                value = CASE
                    WHEN excluded.price_coin > 0 AND excluded.price_coin <> guard_events.price_coin
                    THEN excluded.value ELSE guard_events.value END
            """,
            (ts, month, uid, uname, guard_level, guard_name, months,
             price_coin, value, start_time, source, raw_json),
        )
        if commit:
            self._conn.commit()
        return cur.rowcount > 0

    def list_guard_events(
        self,
        date_from: str = "",
        date_to: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """按时间倒序列出上舰记录。"""
        where = ["1=1"]
        params: list[Any] = []
        lo, hi = _date_bounds(date_from, date_to)
        if lo is not None:
            where.append("ts >= ?")
            params.append(lo)
        if hi is not None:
            where.append("ts <= ?")
            params.append(hi)
        params.append(limit)
        rows = self._conn.execute(
            f"""
            SELECT ts, uid, uname, guard_level, guard_name, months, price_coin, value, start_time
            FROM guard_events WHERE {' AND '.join(where)}
            ORDER BY ts DESC LIMIT ?
            """,
            params,
        ).fetchall()
        keys = ("ts", "uid", "uname", "guard_level", "guard_name", "months",
                "price_coin", "value", "start_time")
        return [dict(zip(keys, row)) for row in rows]

    # ── 历史数据导入（录播弹幕 XML）────────────────────────────────

    def live_gift_index(self, start_ts: int, end_ts: int) -> dict[tuple[int, str, int], list[int]]:
        """区间内【实时记录】的礼物，按 (uid, 礼物名, 数量) 建索引，用于识别导入重叠。"""
        rows = self._conn.execute(
            """
            SELECT uid, gift_name, gift_num, ts FROM gift_events
            WHERE dedupe_key = '' AND ts BETWEEN ? AND ?
            """,
            (start_ts, end_ts),
        ).fetchall()
        idx: dict[tuple[int, str, int], list[int]] = {}
        for uid, name, num, ts in rows:
            idx.setdefault((int(uid), str(name), int(num)), []).append(int(ts))
        return idx

    def guard_times(self, start_ts: int, end_ts: int) -> list[tuple[int, int, int]]:
        """区间内已有的上舰记录 (uid, guard_level, ts)。"""
        rows = self._conn.execute(
            "SELECT uid, guard_level, ts FROM guard_events WHERE ts BETWEEN ? AND ?",
            (start_ts, end_ts),
        ).fetchall()
        return [(int(a), int(b), int(c)) for a, b, c in rows]

    def rebuild_monthly_stats(self, months: list[str] | None = None) -> int:
        """按 gift_events 明细重算月度聚合。

        用于「按日期删除」这种会部分删掉某些月份的场合：聚合是增量累加的，
        删了几行之后不重算就会比明细偏大，越删越离谱。
        months=None 表示全量重算（月份很多时慢，只用于修复）。
        返回重算的月份数。
        """
        if months is None:
            months = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT month FROM monthly_gift_stats "
                "UNION SELECT DISTINCT month FROM monthly_blindbox_stats "
                "UNION SELECT DISTINCT month FROM gift_events")]
        months = [m for m in months if m]
        if not months:
            return 0
        # 只重算「明细还在」的月份。保留期清理是整月删聚合、但可能只删掉某月的部分明细；
        # 如果某个日期区间的明细已经被清空而聚合还在，按明细重算会把它抹成 0 —— 那是数据丢失。
        # 所以这里主动把「没有任何明细」的月份排除掉（它们原样保留，交给清理逻辑决定）。
        placeholders0 = ",".join("?" for _ in months)
        has_detail = {r[0] for r in self._conn.execute(
            f"SELECT DISTINCT month FROM gift_events WHERE month IN ({placeholders0})", months)}
        months = [m for m in months if m in has_detail]
        if not months:
            return 0
        placeholders = ",".join("?" for _ in months)
        self._conn.execute(
            f"DELETE FROM monthly_gift_stats WHERE month IN ({placeholders})", months)
        self._conn.execute(
            f"DELETE FROM monthly_blindbox_stats WHERE month IN ({placeholders})", months)
        # 与 record_gift_event 的累加口径保持一致：
        #   gift_count = 行数、gift_num_total = SUM(gift_num)、value_total = SUM(actual_value)
        self._conn.execute(
            f"""
            INSERT INTO monthly_gift_stats
                (month, uid, uname, gift_count, gift_num_total, value_total, last_ts)
            SELECT month, uid,
                   (SELECT uname FROM gift_events g2 WHERE g2.month = g.month AND g2.uid = g.uid
                    ORDER BY id DESC LIMIT 1),
                   COUNT(1), COALESCE(SUM(gift_num), 0), COALESCE(SUM(actual_value), 0),
                   COALESCE(MAX(ts), 0)
            FROM gift_events g
            WHERE month IN ({placeholders})
            GROUP BY month, uid
            """, months)
        self._conn.execute(
            f"""
            INSERT INTO monthly_blindbox_stats
                (month, uid, uname, blind_box_count, cost_total, actual_total, profit_total, last_ts)
            SELECT month, uid,
                   (SELECT uname FROM gift_events g2 WHERE g2.month = g.month AND g2.uid = g.uid
                    ORDER BY id DESC LIMIT 1),
                   COALESCE(SUM(gift_num), 0), COALESCE(SUM(blind_box_cost), 0),
                   COALESCE(SUM(actual_value), 0), COALESCE(SUM(profit_value), 0),
                   COALESCE(MAX(ts), 0)
            FROM gift_events g
            WHERE month IN ({placeholders}) AND is_blind_box = 1
            GROUP BY month, uid
            """, months)
        self._conn.commit()
        return len(months)

    def list_import_sources(self) -> list[dict[str, Any]]:
        """已导入的批次（按 source 归并），供「撤销导入」列表用。"""
        out: list[dict[str, Any]] = []
        rows = self._conn.execute(
            """
            SELECT source, COUNT(1) AS events, COALESCE(SUM(gift_num), 0) AS num,
                   MIN(ts) AS first_ts, MAX(ts) AS last_ts
            FROM gift_events
            WHERE source LIKE 'xml:%'
            GROUP BY source ORDER BY last_ts DESC
            """
        ).fetchall()
        for source, events, num, first_ts, last_ts in rows:
            guard = self._conn.execute(
                "SELECT COUNT(1) FROM guard_events WHERE source = ?", (source,)
            ).fetchone()[0]
            # source 形如 xml:文件名#内容摘要；摘要只用于区分同名文件，界面默认不显示
            raw_name = str(source)[4:] or str(source)
            name, _, short = raw_name.partition("#")
            out.append({
                "source": source,
                "name": name or raw_name,
                "short": short,
                "gift_events": int(events),
                "gift_num": int(num),
                "guard_events": int(guard),
                "first_ts": int(first_ts or 0),
                "last_ts": int(last_ts or 0),
            })
        # 同名批次同时存在时，把摘要补到名字后面，否则两个「danmaku.xml」看着一模一样
        seen: dict[str, int] = {}
        for item in out:
            seen[item["name"]] = seen.get(item["name"], 0) + 1
        for item in out:
            if seen.get(item["name"], 0) > 1 and item["short"]:
                item["name"] = f"{item['name']} ({item['short']})"
        return out

    def delete_gift_events_by_source(self, source: str) -> dict[str, int]:
        """撤销一次导入：按行精确扣回月度聚合，再删行（不重算，避免与保留期清理打架）。"""
        rows = self._conn.execute(
            """
            SELECT month, uid, is_blind_box, gift_num, blind_box_cost,
                   actual_value, profit_value, ts
            FROM gift_events WHERE source = ?
            """,
            (source,),
        ).fetchall()
        if not rows:
            return {"deleted": 0, "guard_deleted": 0}

        blind_delta: dict[tuple[str, int], list[int]] = {}
        gift_delta: dict[tuple[str, int], list[int]] = {}
        for month, uid, is_blind, num, cost, actual, profit, _ts in rows:
            g = gift_delta.setdefault((month, uid), [0, 0, 0, 0])
            g[0] += 1
            g[1] += int(num)
            g[2] += int(actual)
            g[3] = max(g[3], int(_ts))
            if is_blind:
                b = blind_delta.setdefault((month, uid), [0, 0, 0, 0])
                b[0] += int(num)
                b[1] += int(cost)
                b[2] += int(actual)
                b[3] += int(profit)

        for (month, uid), d in gift_delta.items():
            self._conn.execute(
                """
                UPDATE monthly_gift_stats
                   SET gift_count = gift_count - ?,
                       gift_num_total = gift_num_total - ?,
                       value_total = value_total - ?
                 WHERE month = ? AND uid = ?
                """,
                (d[0], d[1], d[2], month, uid),
            )
        for (month, uid), d in blind_delta.items():
            self._conn.execute(
                """
                UPDATE monthly_blindbox_stats
                   SET blind_box_count = blind_box_count - ?,
                       cost_total = cost_total - ?,
                       actual_total = actual_total - ?,
                       profit_total = profit_total - ?
                 WHERE month = ? AND uid = ?
                """,
                (d[0], d[1], d[2], d[3], month, uid),
            )
        # 扣到全 0 的行直接删掉，别在排行榜里留空壳
        self._conn.execute(
            "DELETE FROM monthly_gift_stats WHERE gift_count <= 0 AND value_total <= 0")
        self._conn.execute(
            "DELETE FROM monthly_blindbox_stats WHERE blind_box_count <= 0 AND cost_total <= 0")

        deleted = self._conn.execute(
            "DELETE FROM gift_events WHERE source = ?", (source,)).rowcount
        guard_deleted = self._conn.execute(
            "DELETE FROM guard_events WHERE source = ?", (source,)).rowcount
        self._conn.commit()
        return {"deleted": int(deleted), "guard_deleted": int(guard_deleted)}

    def get_guard_totals(self, date_from: str = "", date_to: str = "") -> dict[str, Any]:
        """上舰数量与价值汇总（value 单位：电池）。"""
        where = ["1=1"]
        params: list[Any] = []
        lo, hi = _date_bounds(date_from, date_to)
        if lo is not None:
            where.append("ts >= ?")
            params.append(lo)
        if hi is not None:
            where.append("ts <= ?")
            params.append(hi)
        row = self._conn.execute(
            f"""
            SELECT COUNT(*), COALESCE(SUM(value), 0),
                   COALESCE(SUM(CASE WHEN guard_level = 3 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN guard_level = 2 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN guard_level = 1 THEN 1 ELSE 0 END), 0)
            FROM guard_events WHERE {' AND '.join(where)}
            """,
            params,
        ).fetchone()
        return {
            "count": int(row[0] or 0),
            "value": int(row[1] or 0),
            "captain": int(row[2] or 0),
            "commander": int(row[3] or 0),
            "governor": int(row[4] or 0),
        }

    def close(self) -> None:
        self._conn.close()

    def delete_events_before(self, before_ts: int) -> dict[str, int]:
        """删除指定时间戳之前的所有数据，返回删除条数."""
        deleted_gift = self._conn.execute(
            "DELETE FROM gift_events WHERE ts < ?", (before_ts,)
        ).rowcount
        deleted_guards = self._conn.execute(
            "DELETE FROM guard_events WHERE ts < ?", (before_ts,)
        ).rowcount

        # 同时清理对应的月度汇总（重新计算）
        # 简单做法：如果该月已无数据则删除汇总行
        # 获取被删除事件涉及的月份
        affected_months = self._conn.execute(
            "SELECT DISTINCT month FROM gift_events WHERE ts >= ?",
            (before_ts,),
        ).fetchall()
        
        deleted_blind = self._conn.execute(
            "DELETE FROM monthly_blindbox_stats WHERE month NOT IN (SELECT DISTINCT month FROM gift_events)"
        ).rowcount
        
        deleted_gift_stats = self._conn.execute(
            "DELETE FROM monthly_gift_stats WHERE month NOT IN (SELECT DISTINCT month FROM gift_events)"
        ).rowcount
        
        self._conn.commit()
        return {
            "deleted_gift_events": deleted_gift,
            "deleted_guard_events": deleted_guards,
            "deleted_blindbox_stats": deleted_blind,
            "deleted_gift_stats": deleted_gift_stats,
        }
