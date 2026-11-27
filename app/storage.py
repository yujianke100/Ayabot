from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from pathlib import Path
from typing import Optional

import logging

from app import tzutil

logger = logging.getLogger(__name__)


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
    """统计存储。

    `STATS_EVENT_TYPE` 是「算进统计的事件类型」的唯一口径。
    历史上 `gift_events.event_type` 存的是原始协议名（SEND_GIFT / COMBO_SEND / …），
    后来又统一成 SEND_GIFT。为了让老库里的连击汇总行（COMBO_SEND）不再污染统计，
    所有聚合/排行/导出一律只看 SEND_GIFT —— 那些行本来就是前面若干条 SEND_GIFT 的
    合计副本（连击汇总金额与逐条之和完全相等，已用真实数据核对）。
    """

    STATS_EVENT_TYPE = "SEND_GIFT"

    def __init__(self, db_path: str) -> None:
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(p)
        self._conn = sqlite3.connect(p)
        # busy_timeout 放最前面：后面每一条 PRAGMA 都可能撞到别的连接（房间机器人、
        # 并发的导入、WebUI 的只读查询）持有的锁。
        self._conn.execute("PRAGMA busy_timeout=8000;")
        self._enable_wal()
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        # SQL 里按天/按月分组要用配置时区，不能用 SQLite 的 localtime（那是系统时区）
        tzutil.install_sqlite_functions(self._conn)
        self._create_tables()

    def _enable_wal(self) -> None:
        """切到 WAL，并**容忍别人先切好了**。

        `PRAGMA journal_mode=WAL` 有一个坑：**它不遵守 busy_timeout**。
        SQLite 在切换日志模式时不会调用 busy handler，另一个连接持有数据库时
        会立刻返回 `database is locked`（不是等 8 秒）。
        实测：两个线程/进程同时首次打开一个**新建**的库，其中一个必然在
        这一行炸掉 —— 而「新建房间后立刻导入/查看统计」时前端会并发开好几个连接，
        于是用户看到「写入失败：database is locked」。

        正确做法是把它当成**幂等的乐观切换**：先读当前模式，已经是 wal 就跳过；
        切换失败就重新读一次，只要现在确实是 wal 就当成功。
        """
        try:
            if str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                return  # 绝大多数情况走这里：库早就是 WAL 了，一次读，不加写锁
        except Exception:  # noqa: BLE001
            pass
        for attempt in range(6):
            try:
                got = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
                if got and str(got[0]).lower() == "wal":
                    return
            except sqlite3.OperationalError:
                pass
            # 可能被别的连接切好了：重读一次，是 wal 就算成功
            try:
                if str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.05 * (attempt + 1))
        # 实在切不过去也不至于让整个房间起不来：回滚日志模式照样能读能写，
        # 只是读写并发差一些。这里不抛，交给上层日志。
        logger.warning("切换 WAL 失败（%s），继续以其他日志模式运行", self._db_path)

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

            -- 按用户查时间区间：外部统计接口（overlay/插件）和 WebUI 的
            -- 「某人的礼物明细」都是 `WHERE uid = ? AND ts >= ? AND ts <= ?`。
            -- 只有 (month, uid) 和 (ts) 两个索引时 SQLite 只能扫全表，
            -- 20 万行实测 single user_stats 要 6.7 秒；有了这个索引是毫秒级。
            CREATE INDEX IF NOT EXISTS idx_gift_events_uid_ts
            ON gift_events(uid, ts);

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
        # 索引是「老库里没有、新库里有」的典型：只在 _create_tables 里加，
        # 已经存在的库不会补建（CREATE TABLE IF NOT EXISTS 不会重跑整段）。
        # 实测 20 万行：没有它时按用户查要 6.7s，有它是毫秒级。
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gift_events_uid_ts ON gift_events(uid, ts)")
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

        只有 `STATS_EVENT_TYPE`（SEND_GIFT）才更新月度聚合：老库里可能残留
        连击汇总行（COMBO_SEND），它们是重复副本，不能让它们再进聚合。
        """
        counts_in_stats = event.event_type == self.STATS_EVENT_TYPE
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

        if event.is_blind_box and counts_in_stats:
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
        if counts_in_stats:
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
            WHERE month = ? AND uid = ? AND event_type = 'SEND_GIFT'
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
            WHERE ts >= ? AND ts < ? AND is_blind_box = 1 AND event_type = 'SEND_GIFT'
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
              AND event_type = 'SEND_GIFT'
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
              AND event_type = 'SEND_GIFT'
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
            WHERE ts >= ? AND ts < ? AND uid = ? AND event_type = 'SEND_GIFT'
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

    def get_danmaku_log(self, limit: int = 50, offset: int = 0, date_from: str = "", date_to: str = "",
                        asc: bool = True, cap: int = 1000) -> list[dict[str, int | str]]:
        # 日期过滤一律换算成 ts 区间：既按配置时区算，又能用上 ts 索引。
        # 不要在 SQL 里写 ayabot_date(ts) —— 那是逐行回调 Python，20 万行要 0.6s，而区间比较是 6ms。
        where, params = _date_range_where(date_from, date_to)
        order_dir = "ASC" if asc else "DESC"
        # SQLite 里负数 LIMIT = 不限（实测 LIMIT -1 返回全表），所以必须夹取。
        # `0` 不是「不限」也不是「0 条」，统一按「没指定」处理 → 默认 50。
        try:
            _lim = int(limit)
        except (TypeError, ValueError):
            _lim = 50
        if _lim <= 0:
            _lim = 50
        # 上限由调用方决定：分页接口用默认 1000 防止一次拖全表；
        # CSV 导出必须传大 cap —— 否则「导出全部弹幕」被静默截断成前 1000 条。
        try:
            _cap = max(1, int(cap))
        except (TypeError, ValueError):
            _cap = 1000
        limit = min(_lim, _cap)
        try:
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            offset = 0
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
                    "DELETE FROM monthly_blindbox_stats WHERE month NOT IN (SELECT DISTINCT month FROM gift_events WHERE event_type = 'SEND_GIFT')"
                )
                self._conn.execute(
                    "DELETE FROM monthly_gift_stats WHERE month NOT IN (SELECT DISTINCT month FROM gift_events WHERE event_type = 'SEND_GIFT')"
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
              AND event_type = 'SEND_GIFT'
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
            f"SELECT DISTINCT month FROM gift_events "
            f"WHERE month IN ({placeholders0}) AND event_type = 'SEND_GIFT'", months)}
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
            WHERE month IN ({placeholders}) AND event_type = 'SEND_GIFT'
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
              AND event_type = 'SEND_GIFT'
            GROUP BY month, uid
            """, months)
        self._conn.commit()
        return len(months)

    # ── 统计自检 / 修复 ─────────────────────────────────────
    #
    # 聚合是**增量**累加的：每次写礼物都会 +1/+num/+value。这条路上任何一次
    # 「写了明细但没写聚合」（旧版本、中断、手工改库、跨版本迁移）都会留下
    # 永久偏差，用户看到的现象就是「排行榜/季度统计跟明细对不上」「统计比实际少」。
    # 无法追溯历史原因，但可以检出并重算 —— 所以提供这两个方法。

    def consistency_report(self, limit: int = 20) -> dict[str, Any]:
        """比对「礼物明细」与「月度聚合」，返回偏差清单。

        只读，不改数据。偏差按 (月份, uid) 聚合，取绝对值最大的若干条。
        """
        detail = {
            (m, u): (int(c), int(n), int(v)) for m, u, c, n, v in self._conn.execute(
                """
                SELECT month, uid, COUNT(1), COALESCE(SUM(gift_num), 0),
                       COALESCE(SUM(actual_value), 0)
                FROM gift_events WHERE event_type = 'SEND_GIFT' GROUP BY month, uid
                """)
        }
        agg = {
            (m, u): (int(c), int(n), int(v)) for m, u, c, n, v in self._conn.execute(
                """
                SELECT month, uid, COALESCE(gift_count, 0), COALESCE(gift_num_total, 0),
                       COALESCE(value_total, 0)
                FROM monthly_gift_stats
                """)
        }
        blind_detail = {
            (m, u): (int(n), int(c), int(a), int(p)) for m, u, n, c, a, p in self._conn.execute(
                """
                SELECT month, uid, COALESCE(SUM(gift_num), 0),
                       COALESCE(SUM(blind_box_cost), 0), COALESCE(SUM(actual_value), 0),
                       COALESCE(SUM(profit_value), 0)
                FROM gift_events WHERE is_blind_box = 1 AND event_type = 'SEND_GIFT'
                GROUP BY month, uid
                """)
        }
        blind_agg = {
            (m, u): (int(n), int(c), int(a), int(p)) for m, u, n, c, a, p in self._conn.execute(
                """
                SELECT month, uid, COALESCE(blind_box_count, 0), COALESCE(cost_total, 0),
                       COALESCE(actual_total, 0), COALESCE(profit_total, 0)
                FROM monthly_blindbox_stats
                """)
        }

        def _diff(detail_map, agg_map) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for key in set(detail_map) | set(agg_map):
                d = detail_map.get(key, tuple(0 for _ in agg_map.get(key, ())))
                a = agg_map.get(key, tuple(0 for _ in d))
                # 两边长度对齐（盲盒 4 项、礼物 3 项）
                if len(d) != len(a):
                    pad = max(len(d), len(a))
                    d = tuple(d) + (0,) * (pad - len(d))
                    a = tuple(a) + (0,) * (pad - len(a))
                if d != a:
                    # 「只剩汇总、明细已被保留期清掉」不算错：那是正常的生命周期。
                    # 单独标出来，不计入 ok，也不参与重算（重算会把它抹成 0）。
                    if sum(abs(x) for x in d) == 0:
                        kind = "aggregate_only"
                    elif sum(abs(x) for x in a) == 0:
                        kind = "detail_only"
                    else:
                        kind = "drift"
                    out.append({"month": key[0], "uid": key[1], "kind": kind,
                                "detail": list(d), "aggregate": list(a),
                                "delta": [x - y for x, y in zip(d, a)]})
            out.sort(key=lambda r: -sum(abs(x) for x in r["delta"]))
            return out

        gift_diff = _diff(detail, agg)
        blind_diff = _diff(blind_detail, blind_agg)
        real = [r for r in gift_diff + blind_diff if r["kind"] != "aggregate_only"]
        return {
            "ok": not real,
            "gift_mismatch_count": len(gift_diff),
            "blindbox_mismatch_count": len(blind_diff),
            "aggregate_only_count": sum(
                1 for r in gift_diff + blind_diff if r["kind"] == "aggregate_only"),
            "gift_mismatch": gift_diff[:limit],
            "blindbox_mismatch": blind_diff[:limit],
            # 需要重算的月份（只剩汇总的月份重算也修不了，不列进来）
            "months": sorted({r["month"] for r in real}),
        }

    def repair_stats(self) -> dict[str, Any]:
        """按明细重算月度聚合，返回修复前后的偏差情况。

        `rebuild_monthly_stats(None)` 会跳过「没有任何明细」的月份，
        所以保留期已经清掉明细的月份不会被抹成 0。
        """
        before = self.consistency_report()
        months = before.get("months") or []
        rebuilt = self.rebuild_monthly_stats(months or None)
        after = self.consistency_report()
        return {"rebuilt_months": int(rebuilt), "before": before, "after": after}

    def list_import_sources(self) -> list[dict[str, Any]]:
        """已导入的批次（按 source 归并），供「撤销导入」列表用。"""
        out: list[dict[str, Any]] = []
        # 必须把 gift_events 和 guard_events 都算进来：只含上舰记录的 XML（没有礼物）
        # 也是合法导入，如果只查 gift_events，这种批次在界面上根本不出现，就撤不掉了。
        # 一次聚合两张表，避免每批再来一次 COUNT（批次多时是 N+1）。
        gift_rows = {
            r[0]: (r[1], r[2], r[3], r[4]) for r in self._conn.execute(
                """
                SELECT source, COUNT(1), COALESCE(SUM(gift_num), 0), MIN(ts), MAX(ts)
                FROM gift_events WHERE source LIKE 'xml:%' GROUP BY source
                """
            ).fetchall()
        }
        guard_rows = {
            r[0]: (r[1], r[2], r[3]) for r in self._conn.execute(
                """
                SELECT source, COUNT(1), MIN(ts), MAX(ts)
                FROM guard_events WHERE source LIKE 'xml:%' GROUP BY source
                """
            ).fetchall()
        }
        for source in set(gift_rows) | set(guard_rows):
            events, num, g_first, g_last = gift_rows.get(source, (0, 0, 0, 0))
            guard, d_first, d_last = guard_rows.get(source, (0, 0, 0))
            first_ts = min([t for t in (g_first, d_first) if t] or [0])
            last_ts = max([t for t in (g_last, d_last) if t] or [0])
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
        # 最近导入/最近的场次排前面（原来是 SQL 的 ORDER BY last_ts DESC）
        out.sort(key=lambda x: (-x["last_ts"], x["name"]))
        # 同名批次同时存在时，把摘要补到名字后面，否则两个「danmaku.xml」看着一模一样
        seen: dict[str, int] = {}
        for item in out:
            seen[item["name"]] = seen.get(item["name"], 0) + 1
        for item in out:
            if seen.get(item["name"], 0) > 1 and item["short"]:
                item["name"] = f"{item['name']} ({item['short']})"
        return out

    def delete_gift_events_by_source(self, source: str) -> dict[str, int]:
        """撤销一次导入：按行精确扣回月度聚合，再删行（不重算，避免与保留期清理打架）。

        整个撤销必须在一个**写事务**里完成。先 SELECT 出要扣的明细、再 DELETE、
        再按差额扣聚合，这三步之间一旦让别的连接提交（导入是分批提交的），
        DELETE 删掉的行数就和 SELECT 时看到的不一样了 —— 聚合会与明细永久对不上。
        （实测：撤销时明细 2000 行，DELETE 删掉 4000 行，聚合仍记着 2000 条/20000 电池，
        再点撤销也修不回来，因为明细已经没了。）
        BEGIN IMMEDIATE 先拿写锁，后面的数据集才是稳定的。
        """
        conn = self._conn
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """
                SELECT month, uid, is_blind_box, gift_num, blind_box_cost,
                       actual_value, profit_value, ts
                FROM gift_events WHERE source = ? AND event_type = 'SEND_GIFT'
                """,
                (source,),
            ).fetchall()
            # 注意：这里**不能**在 rows 为空时直接 return —— 只含上舰记录的批次
            # 在 gift_events 里一行都没有，直接返回就撤不掉那批上舰记录了。
            if not rows:
                guard_deleted = conn.execute(
                    "DELETE FROM guard_events WHERE source = ?", (source,)).rowcount
                conn.commit()
                return {"deleted": 0, "guard_deleted": int(guard_deleted)}

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
                conn.execute(
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
                conn.execute(
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
            conn.execute(
                "DELETE FROM monthly_gift_stats WHERE gift_count <= 0 AND value_total <= 0")
            conn.execute(
                "DELETE FROM monthly_blindbox_stats WHERE blind_box_count <= 0 AND cost_total <= 0")

            deleted = conn.execute(
                "DELETE FROM gift_events WHERE source = ?", (source,)).rowcount
            guard_deleted = conn.execute(
                "DELETE FROM guard_events WHERE source = ?", (source,)).rowcount
            conn.commit()
            return {"deleted": int(deleted), "guard_deleted": int(guard_deleted)}
        except BaseException:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise

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
        """删除指定时间戳之前的所有数据，返回删除条数。

        **边界月份必须重算聚合**：月度汇总（`monthly_*_stats`）是增量累加的，
        只删掉「已经没有明细的月份」是不够的 —— 部分删除的月份里，汇总仍然
        把已删除的那部分算在里面，于是聚合永远大于明细（第 5/12 轮同样的问题）。
        受影响月份必须在删除**之前**取（删完就查不到了）。
        """
        affected = [r[0] for r in self._conn.execute(
            "SELECT DISTINCT month FROM gift_events WHERE ts < ?", (before_ts,)
        ).fetchall()]
        affected_guard = [r[0] for r in self._conn.execute(
            "SELECT DISTINCT month FROM guard_events WHERE ts < ?", (before_ts,)
        ).fetchall()]

        deleted_gift = self._conn.execute(
            "DELETE FROM gift_events WHERE ts < ?", (before_ts,)
        ).rowcount
        deleted_guards = self._conn.execute(
            "DELETE FROM guard_events WHERE ts < ?", (before_ts,)
        ).rowcount

        if deleted_gift and affected:
            self.rebuild_monthly_stats(sorted(set(affected)))

        # 整月已经没有明细的汇总行直接删掉（rebuild 之后这里通常已经没东西可删）
        deleted_blind = self._conn.execute(
            "DELETE FROM monthly_blindbox_stats WHERE month NOT IN (SELECT DISTINCT month FROM gift_events WHERE event_type = 'SEND_GIFT')"
        ).rowcount

        deleted_gift_stats = self._conn.execute(
            "DELETE FROM monthly_gift_stats WHERE month NOT IN (SELECT DISTINCT month FROM gift_events WHERE event_type = 'SEND_GIFT')"
        ).rowcount

        self._conn.commit()
        return {
            "deleted_gift_events": deleted_gift,
            "deleted_guard_events": deleted_guards,
            "deleted_blindbox_stats": deleted_blind,
            "deleted_gift_stats": deleted_gift_stats,
        }
