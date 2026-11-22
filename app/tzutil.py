"""全项目统一的时区口径。

**问题背景**
以前这个项目里同时存在三套时间算法，用户机器时区不是东八区时会各自飘：

1. `app/bot.py` 的 `_now()` —— 用 `config.runtime.timezone`（默认 Asia/Shanghai），
   写 `gift_events.month` / `guard_events.month` / `anchor_stream_dates.date` 都是它。
2. `app/storage.py` 的 `month_of_ts()` / `datetime.now()` —— 用**系统本地时区**。
3. 所有 SQL 里的 `date(ts, 'unixepoch', 'localtime')` —— SQLite 的 `localtime`
   也是**系统本地时区**（跟配置完全无关），还有一处 `date(ts, 'unixepoch')`
   干脆是 **UTC**。

结果是同一份数据在「按月统计」（配置时区）和「按日期筛选 / 导出 / 删除」
（系统时区 / UTC）之间对不上，跨天、跨月的场次会错位。

**本模块的口径**
所有「今天 / 某天 / 某月」一律用 `config.runtime.timezone`：

- Python 侧：`now()` / `today_str()` / `day_str()` / `month_str()` / `day_range()`
- SQL 侧：连接上注册 `ayabot_date(ts)` 与 `ayabot_month(ts)`，替代
  `date(ts,'unixepoch','localtime')`。凡是按天分组/过滤的 SQL 都必须用这两个函数，
  不要再写 `localtime`。

时区在进程启动时由配置设定一次（`set_config_timezone`），之后全局只读。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, tzinfo

_TZ_NAME = "Asia/Shanghai"
_TZ: tzinfo | None = None
_TZ_READY = False


def _load_tz(name: str) -> tzinfo | None:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(name))
    except Exception:
        return None


def set_config_timezone(name: str | None) -> None:
    """由配置初始化（`config.runtime.timezone`）。解析失败退回系统本地时区。"""
    global _TZ_NAME, _TZ, _TZ_READY
    _TZ_NAME = (str(name) if name else "Asia/Shanghai").strip() or "Asia/Shanghai"
    _TZ = _load_tz(_TZ_NAME)
    _TZ_READY = True


def get_tz() -> tzinfo | None:
    global _TZ_READY, _TZ
    if not _TZ_READY:
        _TZ = _load_tz(_TZ_NAME)
        _TZ_READY = True
    return _TZ


def tz_name() -> str:
    return _TZ_NAME


def now() -> datetime:
    """配置时区的当前时间（带 tzinfo；解析不到时区时退回本地时间）。"""
    tz = get_tz()
    return datetime.now(tz) if tz else datetime.now()


def today_str() -> str:
    return now().strftime("%Y-%m-%d")


def day_str(ts: int | float) -> str:
    """时间戳 → 配置时区的 YYYY-MM-DD。"""
    return datetime.fromtimestamp(int(ts), get_tz()).strftime("%Y-%m-%d")


def month_str(ts: int | float | None = None) -> str:
    """时间戳（默认当前）→ 配置时区的 YYYY-MM。"""
    if ts is None:
        return now().strftime("%Y-%m")
    return datetime.fromtimestamp(int(ts), get_tz()).strftime("%Y-%m")


def day_range(date_str: str) -> tuple[int, int]:
    """`YYYY-MM-DD`（按配置时区解释）→ 当天的 [起, 止] 时间戳，闭区间。"""
    d = datetime.strptime(date_str.strip()[:10], "%Y-%m-%d")
    tz = get_tz()
    start = d.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return int(start.timestamp()), int(end.timestamp())


def day_start_ts(dt: datetime | None = None) -> int:
    """配置时区下「当天 00:00」的时间戳。

    不要写成 `datetime(d.year, d.month, d.day).timestamp()` —— 那是 **naive** datetime，
    `.timestamp()` 会按**系统本地时区**解释它。系统时区不是配置时区时，取到的「今天零点」
    会整体偏移（东八区 vs UTC 差 8 小时），`#今日盲盒` 这类按「今天」统计的功能就错窗口。
    """
    d = dt or now()
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def day_end_ts(dt: datetime | None = None) -> int:
    """配置时区下「当天 23:59:59」之后一刻的时间戳（当天上界，开区间）。"""
    return day_start_ts(dt) + 86400


def range_before(date_str: str) -> int:
    """`YYYY-MM-DD`（配置时区）当天 00:00:00 的时间戳，用于「删除某日之前」。"""
    return day_range(date_str)[0]


def period_range(period: str) -> tuple[int, int]:
    """today / week / month / all → [起, 止] 时间戳（配置时区，周一为一周起点）。"""
    n = now()
    tz = get_tz()
    if period == "today":
        start = n.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp()), int(time.time())
    if period == "week":
        monday = (n - timedelta(days=n.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return int(monday.timestamp()), int(time.time())
    if period == "month":
        start = n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp()), int(time.time())
    return 0, int(time.time())


# ── SQL 函数 ──────────────────────────────────────────────────────

def install_sqlite_functions(conn) -> None:
    """在 sqlite 连接上注册时区函数，供 SQL 里按配置时区分组/过滤。

    用法：把 `date(ts, 'unixepoch', 'localtime')` 换成 `ayabot_date(ts)`，
    把 `strftime('%Y-%m', ts, 'unixepoch', 'localtime')` 换成 `ayabot_month(ts)`。
    """
    conn.create_function("ayabot_date", 1, _sql_date, deterministic=True)
    conn.create_function("ayabot_month", 1, _sql_month, deterministic=True)


def _sql_date(ts) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), get_tz()).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


def _sql_month(ts) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), get_tz()).strftime("%Y-%m")
    except (ValueError, OSError, OverflowError):
        return None
