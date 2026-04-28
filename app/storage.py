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
        self._cleanup_old_months()

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
                PRIMARY KEY(month, uid)
            );
            """
        )
        self._conn.commit()

    def _cleanup_old_months(self) -> None:
        current_month = datetime.now().strftime("%Y-%m")
        deleted_gifts = self._conn.execute("DELETE FROM gift_events WHERE month < ?", (current_month,)).rowcount
        deleted_stats = self._conn.execute("DELETE FROM monthly_blindbox_stats WHERE month < ?", (current_month,)).rowcount
        if deleted_gifts > 0 or deleted_stats > 0:
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
                    cost_total, actual_total, profit_total
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(month, uid) DO UPDATE SET
                    uname=excluded.uname,
                    blind_box_count=monthly_blindbox_stats.blind_box_count + excluded.blind_box_count,
                    cost_total=monthly_blindbox_stats.cost_total + excluded.cost_total,
                    actual_total=monthly_blindbox_stats.actual_total + excluded.actual_total,
                    profit_total=monthly_blindbox_stats.profit_total + excluded.profit_total
                """,
                (
                    event.month,
                    event.uid,
                    event.uname,
                    event.gift_num,
                    event.blind_box_cost,
                    event.actual_value,
                    event.profit_value,
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

    def close(self) -> None:
        self._conn.close()
