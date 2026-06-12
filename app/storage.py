from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


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


class StatsStore:
    def __init__(self, db_path: str) -> None:
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(p)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
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
            """
        )
        self._conn.commit()

    @staticmethod
    def month_of_ts(ts: int) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m")

    def record_gift_event(self, event: GiftEvent) -> None:
        self._conn.execute(
            """
            INSERT INTO gift_events (
                ts, month, uid, uname, event_type, gift_name, gift_num,
                is_blind_box, blind_box_cost, actual_value, profit_value, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )

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

        self._conn.commit()

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
        today = datetime.now().strftime("%Y-%m-%d")
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

    def close(self) -> None:
        self._conn.close()

    def delete_events_before(self, before_ts: int) -> dict[str, int]:
        """删除指定时间戳之前的所有数据，返回删除条数."""
        deleted_gift = self._conn.execute(
            "DELETE FROM gift_events WHERE ts < ?", (before_ts,)
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
            "deleted_blindbox_stats": deleted_blind,
            "deleted_gift_stats": deleted_gift_stats,
        }
