from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from collections import OrderedDict
from contextlib import suppress
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Python < 3.9 fallback
    ZoneInfo = None  # type: ignore[assignment,misc]

from bilibili_api import Credential, live, user
from bilibili_api.utils.danmaku import Danmaku

from .config import AppConfig, HEARTBEAT_FILE, KeywordRule
from .gift_v2 import decode_send_gift_v2
from .storage import GiftEvent, StatsStore

# 机器人心跳文件名见 app.config.HEARTBEAT_FILE（auth.py 在等待扫码时也会写）
# 心跳写入间隔（秒）
_HEARTBEAT_INTERVAL = 15.0
# 弹幕连接守护间隔（秒）
_DANMAKU_GUARD_INTERVAL = 60.0

#: 多久没收到**任何**事件就判定连接假死并强制重连。
#: B站 服务端每 ~30 秒回一次心跳包（op=5），库里会把它 dispatch 成 "VIEW" 事件，
#: 所以健康的连接至少每 30 秒就会踩一次 `_on_all_events`。
#: 取 180 秒 = 6 个心跳周期，既不会误杀，又能在假死后 3 分钟内自愈。
#:
#: 为什么必须有这条：库自带的心跳超时检测依赖 `ws_recv` 抛异常或
#: `ws_close` 能把阻塞中的接收打断。半开连接（NAT 超时、Wi-Fi 漫游、休眠恢复）
#: 下这两件事都可能不发生 —— Task 永远活着、永远等不到数据，
#: 而守护进程只看「进程活着 + 心跳文件在更新」，于是永远不重启。
#: 用户看到的正是「机器人开着但什么都不收，点一下重启就好了」。
_DANMAKU_SILENCE_TIMEOUT = 180.0
# 后台任务（发消息/定时消息/心跳/凭证）巡检间隔（秒）
_BG_GUARD_INTERVAL = 30.0
# 以 uid 为键的内存表上限。机器人要连续跑几个月，这些表不清就会一直涨：
# 实测 1 万 UID × 10 条 AI 上下文 ≈ 20MB，一个热闹直播间跑半个月就能上百 MB。
# 32/64 位 Windows 上内存涨到一定程度会 MemoryError → 进程直接退出，
# 用户看到的就是「机器人莫名其妙关了」。
_MAX_TRACKED_UIDS = 3000            # 冷却/点赞等纯标量表（每条 8~24 字节）
#: 出站去重键 `_pending_texts` 的上限。正常运行时它 ≈ 队列长度（几十条），
#: 只有泄漏才会涨上去；这里给一个远大于正常值的兜底。
_MAX_PENDING_TEXTS = 2000
_MAX_CHAT_CONTEXTS = 800            # AI 上下文（每条上限 10 条消息，实际占用大得多）
_MAX_CONTEXT_MESSAGES = 50          # 单个上下文最多保留多少条（配置写大了也夹住）
# 同一后台任务在窗口内最多重建多少次；超了就放弃（避免永久崩溃的任务
# 每 30 秒重建一次、日志被刷爆、CPU 白烧）
_BG_MAX_RESTARTS = 5
_BG_RESTART_WINDOW = 300.0
# 凭证热更新检查间隔（秒）
_CREDENTIAL_CHECK_INTERVAL = 15.0


@dataclass(slots=True)
class OutboundMessage:
    text: str
    reply_uid: Optional[int]
    retry_count: int = 0


class LiveRobot:
    def __init__(
        self,
        config: AppConfig,
        credential: Optional[Credential] = None,
        auth: Any = None,
    ) -> None:
        self.config = config
        self.logger = logging.getLogger("bili-live-robot")
        # AuthManager（可选）：用于凭证失效后引导重新登录 / 热更新凭证
        self.auth = auth

        self.credential = credential or Credential(
            sessdata=config.credential.sessdata,
            bili_jct=config.credential.bili_jct,
            buvid3=config.credential.buvid3,
            dedeuserid=config.credential.dedeuserid,
        )

        self.live_room = live.LiveRoom(
            room_display_id=config.room_display_id,
            credential=self.credential,
        )

        self.store = StatsStore(config.storage.sqlite_path)
        # 启动时清理过期数据（依据配置的保留天数）
        try:
            result = self.store.cleanup_old_data(
                gift_days=config.features.gift_retention_days,
                danmaku_days=config.features.danmaku_retention_days if config.features.danmaku_log_enabled else 0,
            )
            summary = ", ".join(f"{k}={v}" for k, v in result.items() if v)
            if summary:
                self.logger.info("startup cleanup: %s", summary)
        except Exception as exc:
            self.logger.warning("startup cleanup failed: %s", exc)

        max_queue = getattr(config.rate_limit, 'max_queue_size', 50)
        self._msg_queue: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=max_queue)
        self._msg_worker_task: Optional[asyncio.Task[None]] = None

        self._danmaku: Optional[live.LiveDanmaku] = None
        self._danmaku_task: Optional[asyncio.Task[None]] = None

        self._last_welcome_ts: dict[int, float] = {}
        self._last_thanks_ts: dict[int, float] = {}
        # 诊断用时间戳：写进心跳文件，便于排查「收不到礼物/弹幕」
        self._last_gift_ts: int = 0
        self._last_danmaku_ts: int = 0
        #: 最近一次收到**任意**事件的时间（含心跳包 → VIEW）。
        #: 假死检测就靠它：健康连接每 ~30s 必然刷新一次。
        self._last_event_ts: float = 0.0
        self._last_uid_warn_ts: float = 0.0
        self._is_live: bool = False
        # 礼物去重：(uid, tid/rnd) -> 最近时间戳（仅用于新协议灰度期防重复计账）
        self._recent_gifts: dict[tuple, float] = {}
        # 上舰去重：(uid, start_time) -> 最近时间戳（一次开通会推送三种事件）
        self._recent_guards: dict[tuple, float] = {}
        #: 弹幕重连互斥（guard / 凭证热更新 / poll 三处都会触发重连）
        self._danmaku_restart_lock: Optional[asyncio.Lock] = None
        # 统计写入失败计数器（写库被锁/磁盘忙时兜底重试仍失败），进心跳供 WebUI 显示
        self._dropped_stats = 0
        # 因缺少 uid 而被丢弃的礼物数 + 最近发生时间（凭证失效/匿名连接的信号）
        self._dropped_no_uid = 0
        self._no_uid_ts: list[float] = []
        # 「收到礼物但没记上」的分项计数（进心跳 → WebUI 横幅）。
        # 用户报「所有统计都是 0」时，必须先能区分「没收到」「解不开」「去重误杀」
        # 「缺 uname 被丢」，否则只能靠猜、靠让用户翻日志。
        self._gift_seen = 0            # 进入统计逻辑的礼物件数
        self._gift_recorded = 0        # 真正写进库的件数
        self._dropped_gift_decode = 0  # V2 的 pb 缺失/解码失败
        self._dropped_gift_dup = 0     # 去重命中（正常，灰度双推）
        self._dropped_gift_no_uname = 0  # 缺 uname（旧代码静默丢弃）
        self._gift_decode_sample = ""  # 第一份解码失败的 pb 片段，给排查用
        # 收到的各类型事件计数（进心跳 + 每 10 分钟打一条 INFO 汇总）。
        # 「统计全是 0」时必须能回答：到底收到了哪些事件？有没有我们不认识的？
        self._event_counts: dict[str, int] = {}
        # 疑似礼物类、但不在已知清单里的事件名（B站改协议时的信号）
        self._unknown_gift_like: set[str] = set()
        # 后台任务异常退出后自动重启的次数（进心跳，便于排查"机器人不说话"）
        self._bg_restarts = 0
        # 每个任务的重建时间戳（限流用），以及已放弃重建的任务名
        self._bg_restart_hist: dict[str, list[float]] = {}
        self._bg_dead: list[str] = []
        # 后台守护任务（run() 里创建）
        self._bg_guard_task: Optional[asyncio.Task[None]] = None
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._danmaku_guard_task: Optional[asyncio.Task[None]] = None
        self._credential_task: Optional[asyncio.Task[None]] = None

        # 待发送集合：键是 (内容, 收件人 uid)，**不能只用内容**。
        # 只用内容会让「两个不同用户在同一时间窗口触发同一条回复」时
        # 第二个人彻底收不到（例如两人都说 #盲盒统计 且都没有记录，
        # 回复文案同为「无盲盒记录」→ 只有第一个入队）。
        # 用户视角就是「机器人不回复我」。
        self._pending_texts: set[tuple[str, Optional[int]]] = set()
        # OrderedDict 用于按「最久未用」淘汰：单纯 dict 在 Python 3.7+ 也有序，
        # 但这里需要显式 move_to_end，用 OrderedDict 语义更清楚
        self._chat_contexts: "OrderedDict[int, list[dict[str, str]]]" = OrderedDict()

        self._admin_uids: set[int] = set()
        self._periodic_task: Optional[asyncio.Task[None]] = None
        self._keyword_reply_cooldown_ts: dict[int, float] = {}

        # 弹幕去重：(uid, text) -> 上次记录时间戳
        self._recent_danmaku: dict[tuple[int, str], float] = {}
        # 欢迎串行化锁（B站 create_task 并发推送多个事件，用锁保证一次只处理一个欢迎）
        self._welcome_lock: asyncio.Lock = asyncio.Lock()

        # PK 状态追踪，防重复触发
        self._in_pk: bool = False

        # 点赞计数: uid -> 累计点赞数（用于 ≥阈值 时一次性感谢）
        self._like_counts: dict[int, int] = {}
        # 分享去重: uid -> 上次感谢时间戳
        self._last_share_ts: dict[int, float] = {}

        # 从配置读取唤醒词
        wake = getattr(config.llm, 'wake_word', 'ayabot')
        _set_wake_word(wake)

        # 记下机器人自己的 UID，用于忽略自己发送的弹幕（防止 AI 回复触发关键词）
        # 优先从 config 获取，否则从 credential cookies 中提取（QR 扫码登录场景）
        self._bot_uid = _safe_int(config.credential.dedeuserid) if config.credential.dedeuserid else 0
        if self._bot_uid == 0 and self.credential is not None:
            try:
                cookies = self.credential.get_cookies()
                self._bot_uid = _safe_int(cookies.get("DedeUserID", 0))
            except Exception:
                pass
        # 根据配置设置时区（影响时段模板匹配）
        set_bot_timezone(config.runtime.timezone)
        self.logger.info("bot uid set to %s (config=%s)", self._bot_uid, config.credential.dedeuserid)

    async def run(self) -> None:
        self.logger.info(
            "robot started, room=%s pid=%s", self.config.room_display_id, os.getpid()
        )
        self._poll_counter = 0
        self._is_live = False
        self._msg_worker_task = asyncio.create_task(self._message_worker())
        self._periodic_task = asyncio.create_task(self._periodic_message_loop())
        # 心跳 / 弹幕守护 / 凭证热更新：解决「进程活着但已掉线」和「凭证失效后只能重启」
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._danmaku_guard_task = asyncio.create_task(self._danmaku_guard_loop())
        self._credential_task = asyncio.create_task(self._credential_watch_loop())
        self._bg_guard_task = asyncio.create_task(self._background_guard_loop())

        try:
            while True:
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # 单次轮询异常绝不能让进程退出 —— 这是 Windows 上「机器人意外关闭」的典型成因
                    self.logger.error("poll loop error: %s", exc, exc_info=True)
                await asyncio.sleep(self.config.runtime.poll_interval_seconds)
        finally:
            await self.shutdown()

    async def _poll_once(self) -> None:
        is_live = await self._is_room_live()
        self._is_live = is_live
        if is_live and self._danmaku is None:
            await self._start_danmaku()
        if (not is_live) and self._danmaku is not None:
            await self._stop_danmaku()

        self._poll_counter = (self._poll_counter + 1) % 10
        if self._poll_counter == 0:
            self.logger.info("poll: live=%s danmaku=%s", is_live, self._danmaku is not None)

    # ── 心跳（WebUI/守护进程据此判断是否需要重启） ───────────

    def _status_path(self) -> Path:
        return Path(self.config.storage.sqlite_path).parent / HEARTBEAT_FILE

    def write_heartbeat(self, phase: str = "running") -> None:
        payload = {
            "ts": int(time.time()),
            "pid": os.getpid(),
            "phase": phase,
            "room_display_id": self.config.room_display_id,
            "live": bool(getattr(self, "_is_live", False)),
            "danmaku_connected": self._danmaku is not None,
            "danmaku_task_alive": bool(
                self._danmaku_task is not None and not self._danmaku_task.done()
            ),
            "last_gift_ts": getattr(self, "_last_gift_ts", 0),
            "last_danmaku_ts": getattr(self, "_last_danmaku_ts", 0),
            # 距离上次收到**任意**事件（含心跳包）多少秒：持续增大 = 连接假死。
            # 健康连接每 ~30 秒就会刷新一次，>180 秒就会被 guard 强制重连。
            "event_silent_seconds": (
                int(time.time() - self._last_event_ts)
                if getattr(self, "_last_event_ts", 0) else -1
            ),
            "queue_depth": self._msg_queue.qsize(),
            # 统计写入失败条数：非 0 说明有礼物/上舰因为写库失败没能记上（日志里有详细信息）
            "dropped_stats": getattr(self, "_dropped_stats", 0),
            # 因缺少 uid（匿名连接/凭证失效）被丢弃的礼物数：非 0 就是「统计全是 0」的原因
            "dropped_no_uid": getattr(self, "_dropped_no_uid", 0),
            # 「收到礼物但没记上」的分项计数：seen 非 0 而 recorded 为 0 时，
            # WebUI 会直接说清楚是哪一类丢的（解码失败 / 缺昵称 / 去重），
            # 而不是让用户看到一排 0 然后猜「统计功能坏了」。
            "gift_seen": getattr(self, "_gift_seen", 0),
            "gift_recorded": getattr(self, "_gift_recorded", 0),
            "gift_dropped_decode": getattr(self, "_dropped_gift_decode", 0),
            "gift_dropped_dup": getattr(self, "_dropped_gift_dup", 0),
            "gift_dropped_no_uname": getattr(self, "_dropped_gift_no_uname", 0),
            # 近期收到的事件类型（Top 12）：排查「收不到礼物」时第一时间看这个
            "event_types": dict(sorted(
                getattr(self, "_event_counts", {}).items(),
                key=lambda kv: kv[1], reverse=True)[:12]),
            "unknown_gift_events": sorted(getattr(self, "_unknown_gift_like", set())),
            # 后台任务异常退出被自动重建的次数：非 0 说明曾经「活着但不说话」
            "bg_restarts": getattr(self, "_bg_restarts", 0),
            # 已放弃重建的任务（必现故障）：非空说明某项功能已彻底停摆
            "bg_dead": list(getattr(self, "_bg_dead", [])),
            "credential_status": (
                self.auth.load_status().get("status") if self.auth is not None else "ok"
            ),
        }
        try:
            path = self._status_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("write heartbeat failed: %s", exc)

    async def _heartbeat_loop(self) -> None:
        ticks = 0
        while True:
            self.write_heartbeat()
            ticks += 1
            # 每 40 个心跳（约 10 分钟）打一条事件汇总。
            # 「统计全是 0」的用户反馈里，最缺的就是这条信息。
            if ticks % 40 == 0:
                self._log_event_summary()
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    # ── 弹幕连接守护 ────────────────────────────────────────

    # ── 后台任务守护（发消息/定时消息/心跳/凭证）──────────────

    #: 需要长期存活的后台任务：(属性名, 重建用的工厂方法名)
    #: 需要被 `_background_guard_loop` 监督的常驻任务。
    #: **新增任何 `asyncio.create_task(常驻协程)` 都必须登记到这里**，
    #: 否则它一旦异常退出就是永久静默失效（进程、心跳、WebUI 全都还显示运行中）。
    #: 自检 `check_bg_task_supervision.py` 会枚举 create_task 的调用点与这张表对账。
    #:
    #: `_danmaku_guard_task` 也必须在表里：它是「连接假死」的唯一检测者，
    #: 它自己死了就再也没人管弹幕连接了（用户看到的就是「开着但不收数据」）。
    #: `_bg_guard_task` 不能登记（没人监督监督者），它的存活由心跳间接体现。
    _BG_TASKS: tuple[tuple[str, str], ...] = (
        ("_msg_worker_task", "_message_worker"),
        ("_periodic_task", "_periodic_message_loop"),
        ("_heartbeat_task", "_heartbeat_loop"),
        ("_credential_task", "_credential_watch_loop"),
        ("_danmaku_guard_task", "_danmaku_guard_loop"),
    )

    async def _background_guard_loop(self) -> None:
        """后台任务异常退出后自动重建。

        为什么需要：`asyncio.create_task()` 起的这些任务**没有人 await**，
        里面抛一次异常它们就永久消失，而进程、心跳、WebUI 状态全都还显示「运行中」。
        最要命的是 `_message_worker`：它一死，机器人在直播间里再也不说一句话
        （感谢、关键词回复、签到、定时消息全部静默失效），但用户看到的是
        「机器人开着但没反应」，很容易被当成「机器人挂了」。
        以前只有弹幕连接有守护，其余四个任务完全没人管。
        """
        while True:
            await asyncio.sleep(_BG_GUARD_INTERVAL)
            try:
                self._check_background_tasks()
                trimmed = self._trim_tracking_tables()
                if trimmed:
                    self.logger.debug("tracking tables trimmed: %s", trimmed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.error("background guard error: %s", exc, exc_info=True)

    def _trim_tracking_tables(self) -> dict[str, int]:
        """给以 uid 为键的内存表封顶，返回每张表被清掉的条数。

        机器人设计上要连续跑几个月。这些表全都按 uid 记录、从不清理：
          - `_last_thanks_ts` / `_keyword_reply_cooldown_ts` / `_last_welcome_ts`：冷却时间戳
          - `_like_counts`：点赞累计
        一个热闹直播间跑半个月就有几万 UID，几十 MB 常驻内存。
        值是时间戳的表按「最旧优先」丢（它们的冷却基本早就过期了，丢了等价于
        「下次重新计时」）；`_like_counts` 按插入顺序丢最旧的。
        宁可偶尔多回一句，也不要内存无限涨到 MemoryError 把进程搞崩。
        """
        trimmed: dict[str, int] = {}
        ts_tables = (
            ("_last_thanks_ts", self._last_thanks_ts),
            ("_keyword_reply_cooldown_ts", self._keyword_reply_cooldown_ts),
            ("_last_welcome_ts", self._last_welcome_ts),
            ("_last_share_ts", self._last_share_ts),
        )
        for name, table in ts_tables:
            over = len(table) - _MAX_TRACKED_UIDS
            if over <= 0:
                continue
            # 按时间戳从小到大丢（最久没触发的先丢）
            victims = sorted(table.items(), key=lambda kv: kv[1])[:over]
            for uid, _ts in victims:
                table.pop(uid, None)
            trimmed[name] = len(victims)

        over = len(self._like_counts) - _MAX_TRACKED_UIDS
        if over > 0:
            victims = list(self._like_counts)[:over]   # dict 保持插入顺序
            for uid in victims:
                self._like_counts.pop(uid, None)
            trimmed["_like_counts"] = over
        # `_pending_texts`（出站去重键）正常由 `_message_worker` 逐条释放，
        # 上限≈队列长度。但任何一次异常/取消都可能留下一条残留，几个月跑下来
        # 只增不减。它是 set（没有顺序），无法做「丢最旧」，
        # 所以超上限时整体清空 —— 后果最多是极端情况下多回一句话，
        # 远好过缓存在内存里无限涨。
        over_pending = len(self._pending_texts) - _MAX_PENDING_TEXTS
        if over_pending > 0:
            self._pending_texts.clear()
            trimmed["_pending_texts"] = over_pending
        return trimmed

    def _check_background_tasks(self) -> list[str]:
        """检查并重建已崩溃的后台任务，返回被重建的任务名。"""
        restarted: list[str] = []
        for attr, factory_name in self._BG_TASKS:
            task = getattr(self, attr, None)
            if task is None or not task.done() or task.cancelled():
                continue
            exc: Optional[BaseException] = None
            try:
                exc = task.exception()
            except Exception:  # noqa: BLE001
                exc = None
            if exc is None:
                # 正常返回（例如定时消息没配置时 `_periodic_message_loop` 直接 return）
                # 不算故障，绝不能重建 —— 否则会变成每 30 秒起一个空任务。
                continue
            # 限流：同一个任务在窗口内反复崩，说明是必现故障（不是偶发网络抖动）。
            # 无限重建只会把日志刷爆、白白烧 CPU，还会掩盖真正的原因，所以放弃并上报。
            now = time.time()
            hist = [t for t in self._bg_restart_hist.get(factory_name, [])
                    if now - t < _BG_RESTART_WINDOW]
            if len(hist) >= _BG_MAX_RESTARTS:
                self._bg_restart_hist[factory_name] = hist
                if factory_name not in self._bg_dead:
                    self._bg_dead.append(factory_name)
                    self.logger.error(
                        "后台任务 %s 在 %d 秒内已崩溃重建 %d 次，放弃重建（最后一次错误：%r）",
                        factory_name, int(_BG_RESTART_WINDOW), len(hist), exc,
                    )
                continue
            hist.append(now)
            self._bg_restart_hist[factory_name] = hist
            self._bg_restarts += 1
            self.logger.error(
                "后台任务 %s 异常退出：%r — 自动重建（第 %s 次）",
                factory_name, exc, self._bg_restarts,
            )
            try:
                new_task = asyncio.create_task(getattr(self, factory_name)())
                setattr(self, attr, new_task)
                restarted.append(factory_name)
            except Exception as exc2:  # noqa: BLE001
                self.logger.error("重建后台任务 %s 失败：%s", factory_name, exc2)
        return restarted

    def _danmaku_silent_seconds(self) -> Optional[float]:
        """距离上次收到任意事件过了多少秒。无记录时返回 None（不参与判定）。"""
        last = getattr(self, "_last_event_ts", 0.0)
        if not last:
            return None
        return max(0.0, time.time() - last)

    def _danmaku_health_action(self) -> Optional[str]:
        """判断弹幕连接是否需要重连；健康返回 None，否则返回原因文案。

        纯函数（只看内存状态），方便自检脚本在不起真实连接的情况下验证判定逻辑。
        覆盖三种故障：

        1. **任务已结束**（库把所有主机都试完了、或 ws 出错退出）→ 重连
        2. **对象在、任务没了**（`_start_danmaku` 中途抛异常留下的半截状态）→ 重连
           这个状态以前是**永久卡死**：`_poll_once` 看到 `_danmaku is not None`
           就不再启动，guard 看到 `task is None` 就 continue，两边都不管，
           而心跳还会报 `danmaku_connected: true`。
        3. **任务活着但长时间收不到任何事件**（半开连接/假死）→ 重连
        """
        if self._danmaku is None:
            return None
        task = self._danmaku_task
        if task is None:
            return "弹幕任务不存在（对象还在）"
        if task.done():
            exc = None
            if not task.cancelled():
                try:
                    exc = task.exception()
                except Exception:  # noqa: BLE001
                    exc = None
            return f"弹幕任务已结束（{exc!r}）" if exc is not None else "弹幕任务已结束"
        if not self._is_live:
            return None          # 没开播时不收数据是正常的
        age = self._danmaku_silent_seconds()
        if age is not None and age > _DANMAKU_SILENCE_TIMEOUT:
            return f"连续 {int(age)} 秒没有收到任何事件（连接假死）"
        return None

    async def _danmaku_guard_loop(self) -> None:
        """弹幕连接的健康检查：任务结束 / 半截状态 / 连接假死，都自动重连。"""
        while True:
            await asyncio.sleep(_DANMAKU_GUARD_INTERVAL)
            try:
                reason = self._danmaku_health_action()
                if reason is None:
                    continue
                self.logger.warning("danmaku guard: %s — reconnecting", reason)
                await self._restart_danmaku()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.error("danmaku guard error: %s", exc, exc_info=True)

    def _restart_lock(self) -> asyncio.Lock:
        """重连互斥锁（懒创建：`__init__` 之后才可能有事件循环）。"""
        lock = getattr(self, "_danmaku_restart_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._danmaku_restart_lock = lock
        return lock

    async def _restart_danmaku(self) -> None:
        """重建弹幕连接（**串行化**）。

        三个地方会同时想重连：健康检查 guard（60s 一轮）、凭证热更新、
        `_poll_once` 发现掉线。它们都在同一个事件循环里，但中间有 await，
        两个重连交错执行会留下「旧的 WS 没关 + 新的 task 被下一个重连覆盖」的
        孤儿连接，越积越多（日志里表现为连上七八条然后突然全掉）。
        用锁保证同一时刻只有一次重连在跑。
        """
        async with self._restart_lock():
            await self._restart_danmaku_locked()

    async def _restart_danmaku_locked(self) -> None:
        try:
            await self._stop_danmaku()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("stop danmaku before restart failed: %s", exc)
        if self._is_live:
            try:
                await self._start_danmaku()
                self.logger.info("danmaku reconnected")
            except Exception as exc:  # noqa: BLE001
                self.logger.error("danmaku reconnect failed: %s", exc, exc_info=True)

    # ── 凭证热更新（运行中失效 → 重新扫码 → 自动恢复） ───────

    #: 这些错误重试没有意义，只会多打几次接口、加速触发限流
    _FATAL_SEND_MARKERS = (
        "1003212",                          # 弹幕超长（B站普通账号 20 字）
        "10031", "频率", "太快", "过快",      # 频率限制：重试只会更糟
        "封禁", "10030", "无权限",            # 被封 / 无发送权限
    )

    @classmethod
    def _is_fatal_send_error(cls, err: str) -> bool:
        """这次发送失败值不值得重试。

        弹幕超长（1003212）重试一百次也一样超长；频率过快（10031）立刻重试
        只会让冷却更久。这两种都必须立刻放弃并给出可操作的日志，
        而不是白白打 9 次接口（3 次内层重试 × 3 次重新入队）。

        超长文案的措辞不固定（「超出限制长度」「弹幕长度超过限制」「超过长度限制」
        「文字太长」…），所以不能只匹配某一个短语：只要同时出现「长/字数」和
        「限制/超出/超过/太长」就按超长处理。
        """
        low = err.lower()
        if any(m.lower() in low for m in cls._FATAL_SEND_MARKERS):
            return True
        about_length = ("长度" in low) or ("字数" in low) or ("太长" in low) or ("超长" in low)
        too_much = any(w in low for w in ("超出", "超过", "限制", "太长", "超长", "过长"))
        return about_length and too_much

    def _note_send_failure(self, err: str) -> None:
        if self.auth is None:
            return
        markers = ("-101", "-403", "未登录", "账号未登录", "not logged in")
        low = err.lower()
        if any(m.lower() in low for m in markers):
            self.auth.mark_needs_login("发送弹幕失败：B站登录状态已失效")

    async def _credential_watch_loop(self) -> None:
        if self.auth is None:
            return
        while True:
            try:
                await asyncio.sleep(_CREDENTIAL_CHECK_INTERVAL)
                new_credential = await self.auth.try_pick_up_new_credential()
                if new_credential is None:
                    continue
                await self._swap_credential(new_credential)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("credential watch error: %s", exc)

    async def _swap_credential(self, credential: Credential) -> None:
        """热更新凭证，并**重建弹幕连接**。

        只替换对象是不够的：WebSocket 的鉴权是在 `connect()` 里用
        `__send_verify_data()` 一次性完成的（见库 `live.py`），连接建立之后
        再改 `credential` 属性对**已经打开的连接毫无作用**。

        真实后果正好对上用户反馈：凭证过期时 B站 会把礼物事件的 uid/uname 抹掉
        （匿名视角），统计因此全是 0 → 用户重新扫码 → 这里只换了对象、
        连接还是旧会话 → **礼物依然没有 uid，统计依然是 0**。
        用户看到的是「重新登录了也没用，重启一下才行」。
        """
        self.credential = credential
        try:
            self.live_room.credential = credential
        except Exception:  # noqa: BLE001
            pass
        if self._danmaku is not None:
            try:
                self._danmaku.credential = credential
            except Exception:  # noqa: BLE001
                pass
        if self.auth is not None:
            self.auth.mark_ok()
        self.logger.info("credential hot-swapped (re-login detected)")
        # 重建连接：让新的凭证真正生效（旧连接可能还是匿名/过期会话）
        if self._danmaku is not None or self._danmaku_task is not None:
            try:
                await self._restart_danmaku()
                self.logger.info("danmaku reconnected with new credential")
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("reconnect after credential swap failed: %s", exc)

    async def shutdown(self) -> None:
        try:
            self.write_heartbeat("stopped")
        except Exception:  # noqa: BLE001
            pass
        await self._stop_danmaku()
        for name in ("_msg_worker_task", "_periodic_task", "_heartbeat_task",
                     "_danmaku_guard_task", "_credential_task", "_bg_guard_task"):
            task = getattr(self, name, None)
            if task is None:
                continue
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._in_pk = False
        self.store.close()

    async def _is_room_live(self) -> bool:
        try:
            info = await self.live_room.get_room_play_info()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("poll room status failed: %s", exc)
            return False

        return _extract_live_status(info)

    def _guarded(self, handler):
        """把事件处理函数包一层 try/except。

        `LiveDanmaku.dispatch()` 是 `asyncio.create_task(handler(...))` —— 处理函数是
        **游离任务**，里面抛异常不会回到任何 await 处：只有 GC 时才由 asyncio 的
        异常处理器打一句「Task exception was never retrieved」，日志里既没有事件类型
        也没有礼物信息，出一个 bug 就等于永久静默丢数据。
        这里统一兜住并打全上下文（事件类型 + 处理函数名 + 原始数据摘要）。
        """
        name = getattr(handler, "__name__", str(handler))

        async def _wrapper(event):
            try:
                await handler(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._dropped_stats += 1
                cmd = event.get("type") if isinstance(event, dict) else None
                try:
                    keys = list((event.get("data") or {}).keys())[:12] if isinstance(event, dict) else []
                except Exception:  # noqa: BLE001
                    keys = []
                self.logger.error(
                    "事件处理异常：handler=%s cmd=%s err=%s keys=%s", name, cmd, exc, keys,
                    exc_info=True)

        return _wrapper

    async def _start_danmaku(self) -> None:
        self._danmaku = live.LiveDanmaku(
            room_display_id=self.config.room_display_id,
            credential=self.credential,
        )

        self._danmaku.on("INTERACT_WORD_V2")(self._guarded(self._on_enter_room))
        self._danmaku.on("INTERACT_WORD_V2")(self._guarded(self._on_interact_word))

        self._danmaku.on("SEND_GIFT")(self._guarded(self._on_gift))
        # 2026-07 起 B 站灰度新协议 SEND_GIFT_V2（protobuf 编码），必须单独监听
        self._danmaku.on("SEND_GIFT_V2")(self._guarded(self._on_gift_v2))
        self._danmaku.on("COMBO_SEND")(self._guarded(self._on_gift))
        self._danmaku.on("UNIVERSAL_EVENT_GIFT")(self._guarded(self._on_gift))
        self._danmaku.on("UNIVERSAL_EVENT_GIFT_V2")(self._guarded(self._on_gift))
        self._danmaku.on("SPECIAL_GIFT")(self._guarded(self._on_special_gift))
        self._danmaku.on("GUARD_BUY")(self._guarded(self._on_guard_buy))
        self._danmaku.on("USER_TOAST_MSG")(self._guarded(self._on_guard_buy))
        self._danmaku.on("USER_TOAST_MSG_V2")(self._guarded(self._on_guard_buy))

        self._danmaku.on("LIKE_INFO_V3_CLICK")(self._guarded(self._on_like))
        self._danmaku.on("ROOM_ADMINS")(self._guarded(self._on_room_admins))
        self._danmaku.on("DANMU_MSG")(self._guarded(self._on_danmaku))

        # Log all events for debugging blindbox and unknown event types
        self._danmaku.on("ALL")(self._guarded(self._on_all_events))

        # Send connected message when WebSocket auth succeeds
        if self.config.features.connected_message_enabled and self.config.features.connected_message:
            self._danmaku.on("VERIFICATION_SUCCESSFUL")(self._guarded(self._on_connected))

        async def _run_connect() -> None:
            assert self._danmaku is not None
            await self._danmaku.connect()

        try:
            self._danmaku_task = asyncio.create_task(_run_connect())
        except Exception:
            # 建 task 失败（事件循环已关等极端情况）时不能留半截状态：
            # `_danmaku` 有值、`_danmaku_task` 为空会让 `_poll_once` 以为
            # 「已经连过」从而永不重试。这里清干净，下一轮 poll 会重新来。
            self._danmaku = None
            raise
        self._last_event_ts = time.time()   # 给它一个宽限期，别刚连上就被判假死
        self.logger.info("danmaku connecting")
        # 记录开播日期
        try:
            self.store.record_stream_date(_now().strftime("%Y-%m-%d"))
        except Exception as exc:  # noqa: BLE001
            # 记日期失败不影响收礼物，绝不能让它把刚建好的连接打回去
            self.logger.warning("record_stream_date failed: %s", exc)

    async def _stop_danmaku(self) -> None:
        if self._danmaku is not None:
            try:
                await self._danmaku.disconnect()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("disconnect failed: %s", exc)

        if self._danmaku_task is not None:
            self._danmaku_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._danmaku_task

        self._danmaku = None
        self._danmaku_task = None

    async def _periodic_message_loop(self) -> None:
        """定时发送提醒消息（支持多模板随机+时段）。"""
        if not self.config.features.periodic_message_enabled:
            return
        interval = max(self.config.features.periodic_message_interval_seconds, 30)

        # 收集可用的定时消息模板
        periodic_texts: list[str] = []
        pm_list = self.config.features.periodic_messages_list
        if pm_list:
            # 多模板列表，运行时按时段过滤 + 随机
            pass  # 在循环内处理
        elif self.config.features.periodic_message_template:
            # 旧版单模板
            periodic_texts = [self.config.features.periodic_message_template]

        if not periodic_texts and not pm_list:
            self.logger.info("periodic message disabled: no template configured")
            return

        self.logger.info("periodic message loop started: interval=%ds templates=%s", interval,
                         len(pm_list) if pm_list else len(periodic_texts))
        while True:
            await asyncio.sleep(interval)
            if self._danmaku is None:
                continue  # 不在直播，跳过

            text = None
            if pm_list:
                # 新版：按时段过滤后随机
                text = _pick_template_from_list(pm_list, logger=self.logger)
            if not text and periodic_texts:
                # 旧版：随机选一条
                text = random.choice(periodic_texts)
            if not text:
                continue

            pkey = (text, None)
            self._pending_texts.discard(pkey)
            self._pending_texts.add(pkey)
            await self._msg_queue.put(OutboundMessage(text=text, reply_uid=None))

    async def _message_worker(self) -> None:
        min_interval = max(self.config.rate_limit.send_interval_seconds, 0.1)
        max_retries_per_msg = 3
        last_sent = 0.0
        consecutive_fails = 0
        current_interval = min_interval
        log_counter = 0

        while True:
            msg = await self._msg_queue.get()

            # Remove from pending set (no longer pending)
            # 注意要在改 text 之前释放，键里带收件人 uid
            self._pending_texts.discard((msg.text, msg.reply_uid))

            # ── 关键词过滤（最外层，覆盖所有消息类型）──
            filtered = self._apply_keyword_filter(msg.text)
            if filtered is None:
                self.logger.debug("keyword_filter dropped in _message_worker: %s", msg.text)
                continue
            msg.text = filtered

            # 截断到 B站允许的长度。
            # 2024-03 起 B站 下线了 UL 成就的「30 字」特权，普通账号**只能发 20 字**，
            # 超长直接拒（code 1003212 超出限制长度）。以前写死 30，
            # 于是默认的「感谢 {uname} 的 {gift_name} x{num}」「本月盲盒共…」在
            # 用户名或金额稍长时全部发送失败 —— 用户看到的是「机器人不回复」。
            max_len = max(int(getattr(self.config.rate_limit, "max_text_len", 20) or 20), 1)
            if len(msg.text) > max_len:
                original_len = len(msg.text)
                # 留一个字符给省略号，别把句子截在半截数字上还看不出被截过
                head = msg.text[: max_len - 1] if max_len >= 2 else msg.text[:max_len]
                msg.text = (head + "…") if max_len >= 2 else head
                self.logger.warning(
                    "message truncated: %d -> %d 字（B站上限 %d）text=%s",
                    original_len, len(msg.text), max_len, msg.text,
                )

            # Wait for rate limit interval
            wait_s = current_interval - (time.time() - last_sent)
            if wait_s > 0:
                await asyncio.sleep(wait_s)

            # Periodically log queue stats
            log_counter += 1
            if log_counter % 20 == 0:
                self.logger.info(
                    "queue stats: depth=%d, interval=%.1fs, consec_fails=%d",
                    self._msg_queue.qsize(), current_interval, consecutive_fails,
                )

            # Attempt to send
            success = False
            try:
                danmaku = Danmaku(text=msg.text)
                retries = self.config.rate_limit.retry_count
                for attempt in range(1 + retries):
                    try:
                        resp = await self.live_room.send_danmaku(danmaku=danmaku)
                    except Exception as exc:  # noqa: BLE001
                        err_str = str(exc)
                        # 登录态失效时给出明确的「重新登录」引导，而不是无限重试
                        self._note_send_failure(err_str)
                        if self._is_fatal_send_error(err_str):
                            # 重试也不可能成功：直接丢掉并说清怎么改，别打 9 次接口
                            self.logger.error(
                                "send danmaku 永久失败（不重试）：reply_uid=%s len=%d "
                                "err=%s text=%s | 如果是超长，请把模板改短（B站上限 %s 字）",
                                msg.reply_uid, len(msg.text), err_str[:120], msg.text,
                                getattr(self.config.rate_limit, "max_text_len", 20),
                            )
                            break
                        # Rate-limited or server error → back off
                        if attempt < retries:
                            backoff = 1.0 * (attempt + 1) + random.uniform(0, 0.5)
                            self.logger.warning(
                                "send danmaku retry %d/%d: reply_uid=%s wait=%.1fs err=%s",
                                attempt + 1, retries, msg.reply_uid, backoff, err_str[:120],
                            )
                            await asyncio.sleep(backoff)
                            continue
                        # All retries exhausted → re-queue or drop
                        if msg.retry_count < max_retries_per_msg:
                            msg.retry_count += 1
                            self.logger.warning(
                                "send danmaku failed, re-queued (retry %d/%d): reply_uid=%s err=%s",
                                msg.retry_count, max_retries_per_msg, msg.reply_uid, err_str[:120],
                            )
                            try:
                                self._msg_queue.put_nowait(msg)
                            except asyncio.QueueFull:
                                self.logger.warning("queue full, dropping message: reply_uid=%s", msg.reply_uid)
                        else:
                            self.logger.warning(
                                "send danmaku dropped after %d retries: reply_uid=%s text=%s err=%s",
                                max_retries_per_msg, msg.reply_uid, msg.text, err_str[:120],
                            )
                        break
                    else:
                        last_sent = time.time()
                        success = True
                        consecutive_fails = 0
                        # Gradually reduce interval back to min after success
                        if current_interval > min_interval:
                            current_interval = max(min_interval, current_interval * 0.8)
                        self.logger.info(
                            "send danmaku success: reply_uid=%s text=%s", msg.reply_uid, msg.text,
                        )
                        break
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "send danmaku unexpected error: reply_uid=%s err=%s",
                    msg.reply_uid, exc,
                )
                self._note_send_failure(str(exc))

            if not success:
                consecutive_fails += 1
                # Adaptive rate limiting: back off on consecutive failures
                if consecutive_fails >= 3:
                    current_interval = min(current_interval * 1.5, 10.0)
                    self.logger.info(
                        "rate limit backoff: interval increased to %.1fs (consec_fails=%d)",
                        current_interval, consecutive_fails,
                    )

    async def _on_room_admins(self, event: dict[str, Any]) -> None:
        payload = event.get("data", {})
        admins = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(admins, list):
            return

        parsed: set[int] = set()
        for item in admins:
            if not isinstance(item, dict):
                continue
            uid = _safe_int(item.get("uid"))
            if uid > 0:
                parsed.add(uid)

        self._admin_uids = parsed

    async def _on_enter_room(self, event: dict[str, Any]) -> None:
        uid, uname = _extract_enter_uid_uname(event)
        if uid <= 0 or not uname:
            return

        # 关注/分享由 _on_interact_word 通过 pb_decoded 处理
        if not self.config.features.welcome_enabled:
            return

        # 使用全局锁串行化欢迎处理：B站通过 create_task 并发推送多个事件，
        # 必须确保同一 UID 的两次欢迎不会同时进入模板选择逻辑。
        # 锁内做 cooldown 二次检查——第一个获得锁的协程会设置 _last_welcome_ts，
        # 后续协程获得锁后检查到仍在冷却期内就返回。
        async with self._welcome_lock:
            now = time.time()
            last = self._last_welcome_ts.get(uid, 0.0)
            if now - last < self.config.cooldown.welcome_user_seconds:
                return
            self._last_welcome_ts[uid] = now

            # Resolve template: uid-specific -> guard-level -> multi-template list -> default
            template = None
            wtfu = self.config.features.welcome_templates_for_uids
            if wtfu:
                # YAML 存储的键可能是字符串，统一转 int 再查
                raw_tpl = None
                if uid in wtfu:
                    raw_tpl = wtfu[uid]
                elif str(uid) in wtfu:
                    raw_tpl = wtfu[str(uid)]
                if raw_tpl:
                    if isinstance(raw_tpl, dict):
                        # 新版：{template, time_start, time_end}
                        ts = int(raw_tpl.get("time_start", 0))
                        te = int(raw_tpl.get("time_end", 23))
                        if _is_in_time_slot(ts, te):
                            template = str(raw_tpl.get("template", ""))
                    elif isinstance(raw_tpl, str):
                        # 旧版：直接的模板字符串
                        template = raw_tpl

            if template is None:
                guard_level = self._get_guard_level(event)
                if guard_level > 0:
                    self.logger.info("guard welcome: uid=%s uname=%s guard_level=%s", uid, uname, guard_level)
                    # 新版大航海多模板列表
                    gw_list = self.config.features.guard_welcome_templates_list
                    if gw_list:
                        level_key = {3: "captain", 2: "commander", 1: "governor"}.get(guard_level)
                        if level_key and level_key in gw_list:
                            template = _pick_template_from_list(gw_list[level_key], uname=uname, logger=self.logger)
                            if template:
                                self.logger.info("guard welcome from multi-list: level=%s template=%s", level_key, template)
                    # 降级到旧版单模板
                    if not template:
                        gwt = self.config.features.guard_welcome_templates
                        if gwt:
                            if guard_level == 3:
                                template = gwt.get("captain")
                            elif guard_level == 2:
                                template = gwt.get("commander")
                            elif guard_level == 1:
                                template = gwt.get("governor")
                            if template:
                                self.logger.info("guard welcome from single template: level=%s template=%s", guard_level, template)
                            else:
                                self.logger.info("guard welcome template not configured for level=%s, fallback to default", guard_level)

            # 荣耀等级欢迎（优先级：高于普通欢迎，低于大航海和 UID 特定）
            if not template:
                hw = self.config.features
                if hw.honor_welcome_enabled:
                    honor_medal_level = self._get_honor_level(event)
                    if honor_medal_level >= hw.honor_welcome_min_level:
                        self.logger.info("honor welcome: uid=%s uname=%s medal_level=%s", uid, uname, honor_medal_level)
                        hw_list = hw.honor_welcome_templates_list
                        if hw_list:
                            template = _pick_template_from_list(hw_list, uname=uname, logger=self.logger)
                        if not template:
                            template = hw.honor_welcome_template

            # 新版普通欢迎多模板列表
            if not template:
                wl = self.config.features.welcome_templates_list
                if wl:
                    template = _pick_template_from_list(wl, uname=uname, logger=self.logger)

            # 最后降级到旧版单模板
            if not template:
                template = self.config.features.welcome_template

            if not template:
                return

            text = template.replace("{uname}", uname)
            # 欢迎文本去重：同一个人重复进房不重复欢迎；
            # 不同人即使模板渲染出完全一样的句子也要各发一次
            wkey = (text, uid)
            if wkey in self._pending_texts:
                self.logger.debug("dedup welcome text: uid=%s text=%s", uid, text)
                return
            self._pending_texts.add(wkey)
            try:
                self._msg_queue.put_nowait(OutboundMessage(text=text, reply_uid=uid))
            except asyncio.QueueFull:
                self._pending_texts.discard(wkey)
                self.logger.warning("queue full, welcome dropped: uid=%s", uid)

    async def _on_interact_word(self, event: dict[str, Any]) -> None:
        """监听 INTERACT_WORD_V2 的 pb_decoded，用于关注/分享检测。"""
        data = event.get("data", {})
        if not isinstance(data, dict):
            return
        # protobuf 解析数据在 event.data.data.pb_decoded 中
        nested = data.get("data") if isinstance(data.get("data"), dict) else data
        pb = nested.get("pb_decoded") if isinstance(nested, dict) else None
        if not isinstance(pb, dict):
            return
        msg_type = int(pb.get("msg_type", 0) or 0)
        uid = _safe_int(pb.get("uid", 0))
        uname = str(pb.get("uname", "") or "")
        if uid <= 0 or not uname or msg_type <= 0:
            return

        if msg_type == 2 and self.config.features.follow_thanks_enabled:
            template = self.config.features.follow_thanks_template
            if template:
                text = template.replace("{uname}", uname)
                await self._enqueue_message(text=text, reply_uid=uid)
        elif msg_type == 3 and self.config.features.share_thanks_enabled:
            template = self.config.features.share_thanks_template
            if template:
                text = template.replace("{uname}", uname)
                await self._enqueue_message(text=text, reply_uid=uid)

    async def _on_like(self, event: dict[str, Any]) -> None:
        """监听点赞事件，B站已聚合触发，每次事件感谢一次。"""
        if not self.config.features.like_thanks_enabled:
            return
        try:
            top = event.get("data", {})
            if not isinstance(top, dict):
                return
            raw_data = top.get("data") if isinstance(top.get("data"), dict) else top

            uid = _safe_int(raw_data.get("uid") or 0)
            uname = str(raw_data.get("uname") or "")

            if uid <= 0 or not uname:
                return

            # 冷却：同用户 60 秒内只感谢一次
            now = time.time()
            last = self._last_thanks_ts.get(uid, 0.0)
            if now - last < 60:
                return
            self._last_thanks_ts[uid] = now

            template = self.config.features.like_thanks_template
            text = template.replace("{uname}", uname)
            self.logger.info("LIKE thanks: %s", text)
            await self._enqueue_message(text=text, reply_uid=uid)
        except Exception as exc:
            self.logger.warning("Error processing LIKE_INFO_V3_CLICK: %s", exc)

    async def _on_gift(self, event: dict[str, Any]) -> None:
        """旧协议礼物事件（SEND_GIFT / COMBO_SEND / UNIVERSAL_EVENT_GIFT*）。"""
        # COMBO_SEND 是**连击汇总**，不是一件新礼物：B站 在连击结束时补发一条，
        # 只带 combo_num / combo_total_coin，没有 giftName / total_coin。
        # 用真实数据核对过（连击汇总金额 == 前面 N 条 SEND_GIFT 之和，误差 0）：
        #   combo_total_coin=2000 ← 10×1000 + 10×1000
        #   combo_total_coin=900  ← 9×100
        # 如果照旧记一条，就会给每个连击多记 1 条 0 元礼物，
        # 把「礼物数」抬高、并在排行榜里塞进一条 0 价值的记录（老版本还会写成名字叫
        # "gift" 的假记录）。所以这里直接跳过统计与致谢。
        if str(event.get("type") or "") == "COMBO_SEND":
            self.logger.debug("COMBO_SEND is a combo summary, skipped (already counted per-item)")
            return

        # UNIVERSAL_EVENT_GIFT_V2 有时与 SEND_GIFT_V2 用同一套 protobuf 字段。
        # 以前这里只按 JSON 解析 → pb 事件被解析成「name=gift / num=1 / 金额 0」的
        # 假礼物：统计里条数在涨、金额永远是 0，比直接丢掉更难排查。
        _inner = (event.get("data") or {})
        _inner = _inner.get("data") if isinstance(_inner, dict) else None
        if isinstance(_inner, dict) and _inner.get("pb"):
            await self._on_gift_v2(event)
            return

        gift = _extract_gift_payload(event)
        if gift is None:
            return
        await self._handle_gift_payload(
            gift["raw"], gift["event_type"], gift_name_fallback=gift["gift_name"]
        )

    async def _on_gift_v2(self, event: dict[str, Any]) -> None:
        """新协议礼物事件 SEND_GIFT_V2（2026-07 起灰度，protobuf 编码）。

        礼物数据在 `data.data.pb`（base64 protobuf），一个事件里可能打包了
        多件礼物（盲盒批量），必须逐件记录，否则统计会少算。
        """
        data = event.get("data")
        inner = data.get("data") if isinstance(data, dict) else None
        pb = inner.get("pb") if isinstance(inner, dict) else None
        if not pb:
            # 没有 pb 不代表没有礼物数据：部分灰度事件把同样的字段平铺在 JSON 里。
            # 以前这里直接 return（debug 日志），于是一整类事件彻底消失。
            flat = _extract_gift_payload(event)
            if flat is not None:
                self.logger.info("SEND_GIFT_V2 没有 pb，改用 JSON 字段解析")
                await self._handle_gift_payload(
                    flat["raw"], "SEND_GIFT", gift_name_fallback=flat["gift_name"])
                return
            self._dropped_gift_decode += 1
            if not self._gift_decode_sample:
                self._gift_decode_sample = json.dumps(
                    inner, ensure_ascii=False)[:120] if inner is not None else ""
            now_mono = time.time()
            if now_mono - getattr(self, "_last_decode_warn_ts", 0.0) > 60:
                self._last_decode_warn_ts = now_mono
                self.logger.warning(
                    "SEND_GIFT_V2 缺少 pb 字段且无法按 JSON 解析，已累计 %s 条礼物没记上。"
                    "keys=%s",
                    self._dropped_gift_decode,
                    list(inner.keys()) if isinstance(inner, dict) else type(inner).__name__,
                )
            return

        payloads = decode_send_gift_v2(pb)
        if not payloads:
            self._dropped_gift_decode += 1
            if not self._gift_decode_sample:
                self._gift_decode_sample = str(pb)[:120]
            # 每 60 秒最多吵一次；但计数每次都加 —— WebUI 靠计数判断「解码坏了」
            now_mono = time.time()
            if now_mono - getattr(self, "_last_decode_warn_ts", 0.0) > 60:
                self._last_decode_warn_ts = now_mono
                self.logger.warning(
                    "SEND_GIFT_V2 解码失败（pb 长度 %s），已累计 %s 条礼物没记上。"
                    "如果是协议改了，需要更新解析；样本：%s",
                    len(pb), self._dropped_gift_decode, str(pb)[:80],
                )
            return

        for idx, payload in enumerate(payloads):
            # 同一批里的第几件：缺 tid 时用它避免去重键撞车（否则只会记第 1 件）
            payload["_v2_index"] = idx
            await self._handle_gift_payload(payload, "SEND_GIFT")

    async def _handle_gift_payload(
        self,
        payload: dict[str, Any],
        event_type: str,
        gift_name_fallback: str = "gift",
    ) -> None:
        uid = _safe_int(payload.get("uid"))
        uname = str(payload.get("uname") or "")
        gift_name = str(
            payload.get("giftName") or payload.get("gift_name") or gift_name_fallback or "gift"
        )
        gift_num = max(_safe_int(payload.get("num") or payload.get("gift_num") or 1), 1)

        if uid <= 0:
            # 未登录（匿名）连接时 B 站会把 uid/uname 抹掉（uname 形如「要***」），
            # 这种事件无法归属到用户，只能丢弃并提示 —— 通常意味着凭证没生效。
            self._warn_gift_without_uid(uname, gift_name)
            return

        # 灰度期间同一件礼物可能同时走新旧协议推送，按 tid/rnd 去重，避免重复计账
        # （只对新协议事件做去重判断：旧协议 COMBO_SEND 可能与 SEND_GIFT 同 tid 但语义不同）
        self._gift_seen += 1
        if not uname:
            # 没有昵称不影响统计归属（uid 才是主键），补一个占位并计数
            self._dropped_gift_no_uname += 1
            uname = f"用户{uid}"
        is_v2 = payload.get("src_protocol") == "SEND_GIFT_V2"
        tid_or_rnd = str(payload.get("tid") or payload.get("rnd") or "")
        # 缺 tid/rnd 时，同一个事件批量里的第 N 件必须参与键，否则
        # 「盲盒一次开 3 个同款」会得到三个完全相同的键 → 只记 1 件。
        dedupe_key = (
            uid,
            tid_or_rnd
            or f"{gift_name}:{gift_num}:{payload.get('timestamp') or 0}:"
               f"{payload.get('_v2_index', -1)}:{payload.get('giftId') or 0}",
        )
        now_mono = time.time()
        seen = self._recent_gifts.get(dedupe_key)
        if is_v2 and seen is not None and now_mono - seen < 120:
            self._dropped_gift_dup += 1
            self.logger.debug("duplicate gift skipped: %s %s", uname, gift_name)
            return
        self._recent_gifts[dedupe_key] = now_mono
        if len(self._recent_gifts) > 2000:
            self._recent_gifts = {
                k: v for k, v in self._recent_gifts.items() if now_mono - v < 300
            }

        self._last_gift_ts = int(time.time())

        self.logger.debug(
            "gift event: type=%s uid=%s gift_name=%s num=%s proto=%s",
            event_type, uid, gift_name, gift_num, payload.get("src_protocol", "json"),
        )

        # 统计与「感谢开关」解耦：即使关闭感谢，也必须记录礼物
        now = time.time()
        blind = _extract_blindbox_profit(payload)
        if not blind["is_blind_box"]:
            self.logger.debug(
                "gift not detected as blindbox: uid=%s gift_name=%s blind_gift=%s",
                uid, gift_name, payload.get("blind_gift"),
            )
        is_blind = 1 if blind["is_blind_box"] else 0
        cost = blind["blind_box_cost"]
        actual = blind["actual_value"]
        profit = blind["profit_value"]

        month = _now().strftime("%Y-%m")
        event_row = GiftEvent(
            ts=int(now),
            month=month,
            uid=uid,
            uname=uname,
            # 统一写 SEND_GIFT：兼容既有排行/导出 SQL（真实协议见 raw_json.src_protocol）
            event_type="SEND_GIFT",
            gift_name=gift_name,
            gift_num=gift_num,
            is_blind_box=is_blind,
            blind_box_cost=cost,
            actual_value=actual,
            profit_value=profit,
            raw_json=json.dumps(payload, ensure_ascii=False),
        )
        stat_result: dict[str, bool] = {}

        def _write_gift() -> None:
            stat_result["ok"] = self.store.record_gift_event(event_row)

        recorded = self._record_stat(
            "礼物统计",
            label=f"uid={uid} {uname!r} {gift_name} x{gift_num} ts={event_row.ts}",
            write=_write_gift,
        )
        # record_gift_event 返回 False = 去重命中（没新增行），别算进「已记上」
        if recorded and stat_result.get("ok", True):
            self._gift_recorded += 1

        if not self.config.features.thanks_enabled:
            return

        # Cooldown only gates the thank-you message, not recording
        # 当 cooldown=0 时跳过（完全依赖限流队列的 send_interval_seconds 控制发送间隔）
        if self.config.cooldown.thanks_user_seconds > 0:
            last = self._last_thanks_ts.get(uid, 0.0)
            if now - last < self.config.cooldown.thanks_user_seconds:
                return
            self._last_thanks_ts[uid] = now

        thanks = (
            self.config.features.thanks_template.replace("{uname}", uname)
            .replace("{gift_name}", gift_name)
            .replace("{gift_num}", str(gift_num))
        )

        if self.config.features.blindbox_enabled and is_blind:
            if profit >= 0:
                thanks += f"，+{profit}电池"
            else:
                thanks += f"，{profit}电池"

        await self._enqueue_message(text=thanks, reply_uid=uid)

    def _record_stat(self, kind: str, *, label: str, write) -> bool:
        """写统计的兜底通道：**任何情况下都不能让一条礼物/上舰静默消失**。

        为什么需要这一层：
        - 导入录播 XML、清理旧数据、按日期删除都会长时间占住 SQLite 写锁；
          实时链路撞上时 `record_gift_event` 会抛 `database is locked`。
        - 这些回调是 `LiveDanmaku.dispatch()` 里 `asyncio.create_task()` 起的**游离任务**，
          异常不会回到任何人的 await 处，只会在 GC 时变成一句
          「Task exception was never retrieved」——用户看到的现象就是「礼物没统计上，
          日志里什么也没有」。
        所以这里自己重试、自己记日志、自己计数（计数进心跳，WebUI 能看到）。
        """
        for attempt in range(3):
            try:
                write()
                return True
            except sqlite3.OperationalError as exc:
                # 被锁/磁盘忙：等一小会儿重试（导入侧已改成小批提交，正常 <100ms 就放锁）。
                # 这里用的是 time.sleep，会挡住事件循环 —— 所以只重试 3 次、总共最多 0.3s，
                # 宁可少等也不能把弹幕循环卡住。
                if attempt == 2:
                    self._dropped_stats += 1
                    self.logger.error(
                        "%s 写入失败（数据库忙，已重试 3 次）：%s | %s", kind, exc, label)
                    return False
                time.sleep(0.1 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                self._dropped_stats += 1
                self.logger.error("%s 写入失败：%s | %s", kind, exc, label, exc_info=True)
                return False
        return False

    def _warn_gift_without_uid(self, uname: str, gift_name: str) -> None:
        """礼物事件缺少 uid：**这就是「统计全是 0」最可能的原因**，要上报。

        B站 在匿名/未登录连接下会把礼物事件的 uid/uname 抹掉，这种事件既无法归属
        也无法统计。以前这里只打一行 warning，于是：
          - 用户在页面上看到「所有统计都是 0」
          - 界面上没有任何提示，日志里也只有一句容易被忽略的 warning
          - 用户完全不知道是凭证失效，只会以为「统计功能坏了」
        连续丢了几条就明确判定凭证不可用，写 auth_status → 前端弹重新扫码引导。
        """
        self._dropped_no_uid = getattr(self, "_dropped_no_uid", 0) + 1
        now = time.time()
        # 15 分钟内累计 3 条就认定凭证有问题（单条可能是异常事件，不急着误报）
        self._no_uid_ts = [t for t in getattr(self, "_no_uid_ts", []) if now - t < 900]
        self._no_uid_ts.append(now)
        if len(self._no_uid_ts) >= 3 and self.auth is not None:
            # 已经是 needs_login 就不要每条礼物都写一次文件（礼物多时是纯磁盘浪费）
            # 注意用内存态 `auth.status`：`load_status()` 每次都读 JSON 文件，
            # 匿名连接下每秒几十条礼物 = 每秒几十次磁盘读。
            if self.auth.status != "needs_login":
                self.auth.mark_needs_login(
                    "礼物事件缺少用户信息（当前是未登录/匿名连接），礼物无法统计。"
                    "请重新扫码登录 B站账号")
        if now - getattr(self, "_last_uid_warn_ts", 0.0) < 60:
            return
        self._last_uid_warn_ts = now
        self.logger.warning(
            "gift ignored: uid missing (uname=%r gift=%r) — 未登录态下 B站会屏蔽用户信息，"
            "已累计 %s 条无法统计的礼物（若持续出现请重新登录 B站账号）",
            uname, gift_name, self._dropped_no_uid,
        )

    async def _on_special_gift(self, event: dict[str, Any]) -> None:
        self.logger.debug(
            "SPECIAL_GIFT event received: data=%s",
            event.get("data"),
        )

    async def _on_guard_buy(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "?")

        guard = _extract_guard_buy_payload(event)
        if guard is None:
            self.logger.warning(
                "guard_buy parse failed: type=%s keys=%s",
                event_type,
                list(((event.get("data") or {}).get("data") or {}).keys())[:12],
            )
            return

        uid = guard["uid"]
        uname = guard["uname"]
        months = guard["months"]
        guard_type = guard["guard_type"]
        guard_name = guard["guard_name"]
        guard_level = guard["guard_level"]
        start_time = int(guard["start_time"])

        # 同一次开通会推送 GUARD_BUY + USER_TOAST_MSG + USER_TOAST_MSG_V2：
        # 记录只写一次（DB 层按 uid+start_time 去重，并会用实付价纠正金额），
        # 感谢文案只发一次。
        dedupe_key = (uid, start_time)
        now = time.time()
        is_duplicate = dedupe_key in self._recent_guards
        self._recent_guards[dedupe_key] = now
        if len(self._recent_guards) > 500:
            self._recent_guards = {
                k: v for k, v in self._recent_guards.items() if now - v < 86400
            }

        # 记录与「感谢开关」解耦：关掉致谢也必须留下上舰记录
        recorded = False

        def _write_guard() -> None:
            nonlocal recorded
            recorded = self.store.record_guard_event(
                ts=int(now),
                month=_now().strftime("%Y-%m"),
                uid=uid,
                uname=uname,
                guard_level=guard_level,
                guard_name=guard_name,
                months=months,
                price_coin=guard["price_coin"],
                value=guard["value"],
                start_time=start_time,
                source=event_type,
                raw_json=json.dumps(
                    (event.get("data") or {}).get("data") or {}, ensure_ascii=False
                ),
            )

        self._record_stat(
            "上舰统计",
            label=f"uid={uid} {uname!r} {guard_name} x{months} start_time={start_time}",
            write=_write_guard,
        )

        self.logger.info(
            "guard buy: uid=%s uname=%s %s x%s 价值=%s电池 start_time=%s type=%s recorded=%s%s",
            uid, uname, guard_name, months, guard["value"], start_time, event_type, recorded,
            " (重复事件，仅纠正金额)" if is_duplicate else "",
        )

        if is_duplicate:
            return

        if not self.config.features.guard_thanks_enabled:
            return

        if self.config.cooldown.thanks_user_seconds > 0:
            last = self._last_thanks_ts.get(uid, 0.0)
            if now - last < self.config.cooldown.thanks_user_seconds:
                return
            self._last_thanks_ts[uid] = now

        if guard_type == "captain":
            template = self.config.features.guard_thanks_template_captain
        elif guard_type == "commander":
            template = self.config.features.guard_thanks_template_commander
        elif guard_type == "governor":
            template = self.config.features.guard_thanks_template_governor
        else:
            template = self.config.features.guard_thanks_template_default

        text = (
            template.replace("{uname}", uname)
            .replace("{months}", str(months))
            # {guard_type} 历史上输出英文键（captain/…），中文模板里读起来很怪，
            # 现在统一输出中文；想用英文键的可用 {guard_type_en}
            .replace("{guard_type}", guard_name)
            .replace("{guard_type_en}", guard_type)
            .replace("{guard_name}", guard_name)
            .replace("{level}", str(guard_level))
            .replace("{value}", str(guard["value"]))
            .replace("{price}", str(round(guard["price_coin"] / 1000, 1)))
        )
        await self._enqueue_message(text=text, reply_uid=uid)

    async def _on_danmaku(self, event: dict[str, Any]) -> None:
        parsed = _parse_danmaku_user_and_text(event)
        if parsed is None:
            self.logger.warning("danmaku parse failed: raw_event_type=%s keys=%s", event.get("type"), list(event.get("data", {}).keys() if event.get("data") else []))
            return

        uid, uname, text, moderator_hint = parsed
        self._last_danmaku_ts = int(time.time())
        self.logger.debug("danmaku received: uid=%s text=%s (anchor_uid=%s)", uid, text, self.config.anchor_uid)

        # ── UID 黑名单 ──
        bl = self.config.features.uid_blacklist
        if bl:
            # 时段规则要按配置时区判断，不能跟系统本地时区走
            h = _now().hour
            for rule in bl:
                if rule.get("uid") == uid:
                    ts = rule.get("time_start", 0)
                    te = rule.get("time_end", 23)
                    if ts <= te:
                        if ts <= h <= te:
                            self.logger.debug("uid_blacklist blocked: uid=%s text=%s", uid, text)
                            return
                    else:  # 跨天（如 22~6）
                        if h >= ts or h <= te:
                            self.logger.debug("uid_blacklist blocked (cross-day): uid=%s text=%s", uid, text)
                            return

        # 忽略自己发的弹幕（AI 回复或系统消息被广播回来时，可能触发关键词规则）
        if self._bot_uid and uid == self._bot_uid:
            self.logger.debug("skip self-sent danmaku: uid=%s text=%s", uid, text)
            return

        if self.config.features.danmaku_log_enabled:
            # 去重：3秒内相同 uid+text 只记录一次（B站可能重复推送弹幕）
            key = (uid, text)
            now_ts = time.time()
            last_ts = self._recent_danmaku.get(key, 0.0)
            if now_ts - last_ts < 3.0:
                self.logger.debug("skip duplicate danmaku: uid=%s text=%s", uid, text)
            else:
                self._recent_danmaku[key] = now_ts
                self._recent_danmaku = {k: v for k, v in self._recent_danmaku.items() if now_ts - v < 30.0}  # 定期清理
                self.store.record_danmaku(
                    ts=int(now_ts),
                    uid=uid,
                    uname=uname,
                    content=text,
                    max_entries=self.config.features.danmaku_log_max_entries,
                )

        # Anchor exclusive reply
        if uid == self.config.anchor_uid:
            content = text.strip()
            for rule in self.config.features.anchor_exclusive_reply:
                triggered = False
                if rule.is_regex:
                    if re.search(rule.trigger_keyword, content):
                        triggered = True
                elif content == rule.trigger_keyword:
                    triggered = True

                if triggered:
                    self.logger.debug("anchor exclusive reply triggered: uid=%s text=%s reply=%s", uid, text, rule.reply_template)
                    await self._enqueue_reply(text=rule.reply_template, reply_uid=uid)
                    return

        command = _parse_command(text, allow_bare=self.config.features.allow_bare_commands)
        if command is None:
            self.logger.debug("danmaku is not command: uid=%s text=%s", uid, text)

            # ── AI回复免#触发弹幕开头匹配唤醒词 ──
            wake = self.config.llm.wake_word or _CURRENT_WAKE_WORD
            if self.config.features.llm_bare_trigger and text.startswith(wake):
                command = ("llm_chat", text[len(wake):].strip())
                self.logger.debug("bare llm trigger matched: uid=%s text=%s", uid, text)
            # ── 包含关键词触发AI回复（需 allow_bare_commands） ──
            elif self.config.features.allow_bare_commands and self.config.features.llm_keyword_trigger and wake in text:
                command = ("llm_chat", text)
                self.logger.debug("keyword llm trigger matched: uid=%s text=%s", uid, text)

        if command is None:
            # Check keyword-based auto-reply
            kr = self.config.features.keyword_reply
            if kr.enabled and kr.rules:
                reply = _match_keyword_rule(text, kr.rules, uid=uid)
                if reply:
                    now = time.time()
                    last = self._keyword_reply_cooldown_ts.get(uid, 0.0)
                    if now - last >= kr.cooldown_seconds:
                        self._keyword_reply_cooldown_ts[uid] = now
                        self.logger.debug("keyword reply matched: uid=%s text=%s reply=%s", uid, text, reply)
                        await self._enqueue_reply(text=reply, reply_uid=uid)

            # 记录所有弹幕到上下文
            self._record_chat_context(text, uname, uid)
            return

        name, arg = command
        self.logger.debug("command parsed: uid=%s name=%s arg=%s", uid, name, arg)

        # 数字转中文辅助
        def _num(s: str) -> str:
            if self.config.features.use_chinese_numbers:
                return _to_chinese_num(s)
            return s

        if name == "blindbox_me":
            self.logger.info("blindbox stats requested: uid=%s arg=%s", uid, arg)
            now = _now()
            month_key = now.strftime("%Y-%m")
            if arg:
                result = self.store.get_user_monthly_blindbox_by_uname(month=month_key, uname=arg)
                if result is None:
                    reply = f"未找到{arg}的盲盒记录"
                    self.logger.info("blindbox reply: %s", reply)
                    await self._enqueue_reply(text=reply, reply_uid=uid)
                    return
                _uid, blind_count, cost_total, actual_total, profit_total = result
                tmpl = self.config.features.blindbox_result_monthly
                text_out = tmpl.replace("{count}", _num(str(blind_count))).replace("{cost}", _num(str(cost_total))).replace("{profit}", _num(str(profit_total)))
            else:
                if uid == self.config.anchor_uid or self._has_control_permission(uid, moderator_hint):
                    total = self.store.get_monthly_total_blindbox(month=month_key)
                    blind_count, cost_total, actual_total, profit_total = total
                    tmpl = self.config.features.blindbox_result_monthly
                    text_out = tmpl.replace("{count}", _num(str(blind_count))).replace("{cost}", _num(str(cost_total))).replace("{profit}", _num(str(profit_total)))
                else:
                    row = self.store.get_user_monthly_blindbox(month=month_key, uid=uid)
                    if row is None:
                        gift_event_count, _ = self.store.get_user_monthly_gift_activity(month=month_key, uid=uid)
                        text_out = self.config.features.blindbox_no_blindbox if gift_event_count > 0 else self.config.features.blindbox_no_gift
                        self.logger.info("blindbox reply: %s", text_out)
                        await self._enqueue_reply(text=text_out, reply_uid=uid)
                        return
                    blind_count, cost_total, actual_total, profit_total = row
                    tmpl = self.config.features.blindbox_result_monthly
                    text_out = tmpl.replace("{count}", _num(str(blind_count))).replace("{cost}", _num(str(cost_total))).replace("{profit}", _num(str(profit_total)))
            # 玻璃心模式：亏损时隐藏真实收益
            if self.config.features.blindbox_glassheart_enabled and profit_total < 0:
                text_out = self.config.features.blindbox_glassheart_reply
            self.logger.info("blindbox reply: %s", text_out)
            await self._enqueue_reply(text=text_out, reply_uid=uid)
            return

        if name == "today_blindbox":
            self.logger.info("today blindbox stats requested: uid=%s arg=%s", uid, arg)
            now = _now()
            # 用配置时区算当天窗口。写成 datetime(y,m,d).timestamp() 会被当成系统本地时区，
            # 系统时区与配置不一致时「今日盲盒」统计的窗口会整体偏移。
            from app import tzutil
            today_start = tzutil.day_start_ts(now)
            today_end = today_start + 86400
            if arg:
                result = self.store.get_today_user_blindbox_by_uname(today_start, today_end, arg)
                if result is None:
                    reply = f"未找到{arg}今日的盲盒记录"
                    self.logger.info("today blindbox reply: %s", reply)
                    await self._enqueue_reply(text=reply, reply_uid=uid)
                    return
                _uid, blind_count, cost_total, actual_total, profit_total = result
                tmpl = self.config.features.blindbox_result_today
                text_out = tmpl.replace("{count}", _num(str(blind_count))).replace("{cost}", _num(str(cost_total))).replace("{profit}", _num(str(profit_total)))
            else:
                if uid == self.config.anchor_uid or self._has_control_permission(uid, moderator_hint):
                    total = self.store.get_today_total_blindbox(today_start, today_end)
                    blind_count, cost_total, actual_total, profit_total = total
                    tmpl = self.config.features.blindbox_result_today
                    text_out = tmpl.replace("{count}", _num(str(blind_count))).replace("{cost}", _num(str(cost_total))).replace("{profit}", _num(str(profit_total)))
                else:
                    row = self.store.get_today_user_blindbox(today_start, today_end, uid)
                    if row is None:
                        gift_event_count, _ = self.store.get_today_user_gift_activity(today_start, today_end, uid)
                        text_out = self.config.features.blindbox_no_blindbox if gift_event_count > 0 else self.config.features.blindbox_no_gift
                        self.logger.info("today blindbox reply: %s", text_out)
                        await self._enqueue_reply(text=text_out, reply_uid=uid)
                        return
                    blind_count, cost_total, actual_total, profit_total = row
                    tmpl = self.config.features.blindbox_result_today
                    text_out = tmpl.replace("{count}", _num(str(blind_count))).replace("{cost}", _num(str(cost_total))).replace("{profit}", _num(str(profit_total)))
            # 玻璃心模式：亏损时隐藏真实收益
            if self.config.features.blindbox_glassheart_enabled and profit_total < 0:
                text_out = self.config.features.blindbox_glassheart_reply
            self.logger.info("today blindbox reply: %s", text_out)
            await self._enqueue_reply(text=text_out, reply_uid=uid)
            return

        if name == "llm_chat":
            self.logger.info("llm chat requested: uid=%s text=%s", uid, arg)
            from app.web.server import get_llm_config
            from app.llm_client import LLMClient

            cfg = get_llm_config()
            if not cfg.get("enabled") or not cfg.get("api_key"):
                self.logger.debug("llm not configured, skipping")
                await self._enqueue_reply(text=_CURRENT_WAKE_WORD + "还没学会说话呢~", reply_uid=uid)
                return

            if not arg:
                wake = _CURRENT_WAKE_WORD
                await self._enqueue_reply(text=f"想和{wake}说什么？例如 #{wake} <聊天内容>", reply_uid=uid)
                return

            client = LLMClient(
                provider=cfg.get("provider", "openai"),
                api_key=cfg.get("api_key", ""),
                base_url=cfg.get("base_url", "https://api.openai.com/v1"),
                model=cfg.get("model", "gpt-4o-mini"),
                temperature=float(cfg.get("temperature", 0.7)),
                top_p=float(cfg.get("top_p", 0.9)),
                max_tokens=int(cfg.get("max_tokens", 150)),
                system_prompt=cfg.get("system_prompt", "你是ayabot，一个可爱温柔的虚拟主播助手。"),
                bot_name=_CURRENT_WAKE_WORD,
            )

            # 对话上下文
            ctx_cfg = cfg.get("context", {})
            ctx_enabled = ctx_cfg.get("enabled", True)
            ctx_mode = ctx_cfg.get("mode", "isolated")  # "isolated" or "merged"
            ctx_content = ctx_cfg.get("content", "llm_only")  # "llm_only" or "all"
            # 与 _record_chat_context 用同一套夹取规则（0/负数/超大都夹到合法区间，
            # 否则 `history[-0:]` 会取到整张表）
            try:
                ctx_max = int(ctx_cfg.get("max_messages", 10))
            except (TypeError, ValueError):
                ctx_max = 10
            ctx_max = max(1, min(ctx_max, _MAX_CONTEXT_MESSAGES))

            history: list[dict[str, str]] = []
            if ctx_enabled:
                ctx_key = 0 if ctx_mode == "merged" else uid
                history = self._chat_contexts.get(ctx_key, [])
                history = history[-ctx_max:] if len(history) > ctx_max else history
                if history:
                    # 读也算「使用过」，让 LRU 淘汰反映真实的活跃度
                    self._chat_contexts.move_to_end(ctx_key)

            reply = await client.chat(
                user_text=arg, uname=uname,
                chat_history=history if history else None,
            )

            if not reply:
                self.logger.info("llm reply: (empty) -> fallback message")
                await self._enqueue_reply(text=_CURRENT_WAKE_WORD + "不知道该怎么回答呢~", reply_uid=uid)
                return

            self.logger.info("llm reply: %s", reply)

            # 保存到上下文
            if ctx_enabled:
                ctx_key = 0 if ctx_mode == "merged" else uid
                history.append({"role": "user", "content": f'用户"{uname}"说: {arg}'})
                history.append({"role": "assistant", "content": reply})
                if len(history) > ctx_max:
                    history = history[-ctx_max:]
                self._chat_contexts[ctx_key] = history

            # Truncate to fit B站 danmaku limit (~40 chars)
            if len(reply) > 37:
                reply = reply[:37] + "..."
            await self._enqueue_reply(text=reply, reply_uid=uid)
            return

        if name == "help":
            self.logger.info("help requested: uid=%s", uid)
            await self._enqueue_reply(
                text=f"#{_CURRENT_WAKE_WORD} #签到 #抽签 #今日盲盒 #本月盲盒 #帮助",
                reply_uid=uid,
            )
            return

        if name == "checkin":
            self.logger.info("checkin command: uid=%s uname=%s", uid, uname)
            days, rank, already = self.store.user_checkin(uid, uname)
            if already:
                msg = f"今天已经签到了哦！连续签到{days}天，排名第{rank}。继续坚持喵~"
            else:
                msg = f"签到成功！连续签到{days}天，排名第{rank}。继续坚持喵~"
            await self._enqueue_reply(text=msg, reply_uid=uid)
            return

        if name == "fortune":
            self.logger.info("fortune drawn: uid=%s", uid)
            # 默认签文（每种类型多条，随机选）
            default_fortunes = {
                "大吉": ["今天运气爆棚，做什么都顺风顺水！", "主播都被你的欧气惊到了！"],
                "中吉": ["运势不错，是个适合发财的好日子。", "心情舒畅，会有好事发生哦。"],
                "小吉": ["平稳的一天，适合静下心来干大事。", "顺其自然，好运自会到来。"],
                "末吉": ["虽然平淡，但健康平安就是最大的福气。", "不要急躁，慢慢来总会好的。"],
                "凶": ["今天适合低调行事，多看看直播转转运。", "别灰心，下次抽签一定是上签！"],
                "大凶": ["生活总有低谷，吃顿好的安慰一下自己吧。", "多发几条弹幕，霉运都会跑掉的。"],
            }
            # 从配置读取自定义签文（支持列表或单字符串）
            cfg_fortunes = getattr(self.config, "custom_fortunes", {}) or {}
            fortunes = []
            type_map = {
                "大吉": "daiji", "中吉": "zhongji", "小吉": "xiaoji",
                "末吉": "moji", "凶": "xiong", "大凶": "daxiong",
            }
            for f_type, f_defaults in default_fortunes.items():
                custom_val = cfg_fortunes.get(type_map.get(f_type, ""), "")
                if isinstance(custom_val, list):
                    # 新版：列表 -> 多条签文
                    texts = [str(t) for t in custom_val if t]
                elif isinstance(custom_val, str) and custom_val:
                    # 旧版：单字符串
                    texts = [custom_val]
                else:
                    texts = f_defaults
                fortunes.append((f_type, texts))
            f_type, jokes = random.choice(fortunes)
            joke = random.choice(jokes)
            msg = f"抽签结果：【{f_type}】！{joke}"
            await self._enqueue_reply(text=msg, reply_uid=uid)
            return

        if not self._has_control_permission(uid, moderator_hint):
            self.logger.debug(
                "command permission denied: uid=%s name=%s moderator_hint=%s",
                uid,
                name,
                moderator_hint,
            )
            return

        if name == "welcome_on":
            self.config.features.welcome_enabled = True
            await self._enqueue_reply(text="已开启欢迎", reply_uid=uid)
            return

        if name == "welcome_off":
            self.config.features.welcome_enabled = False
            await self._enqueue_reply(text="已关闭欢迎", reply_uid=uid)
            return

        if name == "welcome_text":
            if not arg:
                await self._enqueue_reply(text="用法：#欢迎 词 <欢迎词模板>", reply_uid=uid)
                return
            self.config.features.welcome_template = arg
            await self._enqueue_reply(text="欢迎词已更新", reply_uid=uid)
            return

    def _bump_event_count(self, event_type: str) -> None:
        """统计事件类型出现次数，并记下「礼物类但不认识」的名字。

        之所以放在 `_on_all_events` 的最前面：那里后面有一串 `return`
        （PK 分支、噪音过滤、已知事件过滤），放在后面就等于大部分事件都数不到。
        """
        self._event_counts[event_type] = self._event_counts.get(event_type, 0) + 1
        if (_looks_like_gift_event(event_type)
                and event_type not in KNOWN_GIFT_EVENTS):
            # 只记名字，不刷日志；汇总时一起打出来
            self._unknown_gift_like.add(event_type)

    def _log_event_summary(self) -> None:
        """每 10 分钟打一条 INFO：收到了哪些事件，有没有不认识的礼物类事件。"""
        counts = self._event_counts
        if not counts:
            self.logger.info("事件统计：过去 10 分钟没有收到任何事件")
            self._event_counts = {}
            return
        top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
        parts = " ".join(f"{k}={v}" for k, v in top)
        gift_total = sum(counts.get(k, 0) for k in KNOWN_GIFT_EVENTS)
        self.logger.info(
            "事件统计（近 10 分钟）：%s%s；其中已知礼物类事件 %d 条",
            parts,
            f" 等 {len(counts)} 类" if len(counts) > len(top) else "",
            gift_total,
        )
        if self._unknown_gift_like:
            # 这条是「统计全是 0」最可能的解释之一，必须显眼
            self.logger.warning(
                "发现**未处理**的礼物类事件：%s —— 如果统计是 0，很可能 B站 换了这个"
                "协议名/字段，请把这行和房间日志发给作者",
                ", ".join(sorted(self._unknown_gift_like)),
            )
        if gift_total == 0:
            self.logger.warning(
                "近 10 分钟没有收到任何已知礼物事件（SEND_GIFT 等）。"
                "如果这期间有人在送礼，说明协议变了或凭证/连接有问题。"
            )
        self._event_counts = {}

    async def _on_all_events(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "?")
        self._bump_event_count(event_type)
        # 必须在这里记：下面有一串 early return（PK / 噪音 / 已知事件），
        # 而心跳包是以 "VIEW" 事件进来的 —— 放到后面就永远数不到。
        self._last_event_ts = time.time()

        # ── 捕获所有 PK 相关事件（不管叫什么名字） ──
        if event_type.startswith("PK_") or event_type.startswith("pk_"):
            self.logger.info("PK event received: type=%s data_keys=%s", event_type,
                             list(event.get("data", {}).keys()) if isinstance(event.get("data"), dict) else "?")

            # PK 开始：PRE_NEW 带对手匹配信息，BATTLE_START 是正式开始
            if ("BATTLE_START" in event_type or "PRE" in event_type) and not self._in_pk:
                self._in_pk = True
                await self._handle_pk_start(event)
            elif "SETTLE" in event_type:
                self._in_pk = False
                await self._handle_pk_end(event)
            elif "PROCESS" in event_type or "PUNISH" in event_type or event_type == "PK_INFO":
                self.logger.debug("PK intermediate event ignored: %s", event_type)
            return

        # Suppress high-frequency / known events to reduce log noise
        noisy_prefixes = ("SUPER_CHAT", "HOT_RANK_", "ONLINE_RANK_",
                          "POPULARITY_")
        if event_type.startswith(noisy_prefixes):
            return
        noisy_exact = ("VIEW", "INTERACT_WORD_V2", "WATCHED_CHANGE",
                       "ROOM_REAL_TIME_MESSAGE_UPDATE", "NOTICE_MSG",
                       "LIVE", "PREPARING", "ENTRY_EFFECT", "ROOM_CHANGE",
                       "COMBO_RESOURCE", "COMBO_SEND", "ANIMATION",
                       "SPECIAL_GIFT", "VERIFICATION_SUCCESSFUL",
                       "UNIVERSAL_EVENT_GIFT", "UNIVERSAL_EVENT_GIFT_V2",
                       "GUARD_BUY", "USER_TOAST_MSG", "USER_TOAST_MSG_V2",
                       "LIKE_INFO_V3_CLICK", "LIKE_INFO_V3_UPDATE",
                       "INTERACT_WORD", "WELCOME", "WELCOME_GUARD", "DANMU_MSG",
                       "STOP_LIVE_ROOM_LIST", "WIDGET_BANNER")
        if event_type in noisy_exact:
            return

        # ── DM_INTERACTION（分享触发，会重复收到，需去重） ──
        if event_type == "DM_INTERACTION":
            if self.config.features.share_thanks_enabled:
                try:
                    top = event.get("data", {})
                    raw_data = top.get("data") if isinstance(top.get("data"), dict) else top
                    # DM_INTERACTION 没有 uid/uname，只有一段 JSON 字符串
                    inner = raw_data.get("data", "{}")
                    if isinstance(inner, str):
                        inner = json.loads(inner)
                    cnt = int(inner.get("cnt", 0) or 0)
                    if cnt <= 0:
                        return
                    # 30 秒内只感谢一次（防重复）
                    now = time.time()
                    last = self._last_share_ts.get(0, 0.0)  # 共用 key 0
                    if now - last < 30:
                        return
                    self._last_share_ts[0] = now
                    template = self.config.features.share_thanks_template
                    # 没有用户名，去掉 {uname} 占位符
                    text = template.replace("{uname}", "").strip()
                    if not text:
                        text = "感谢分享直播间~"
                    await self._enqueue_message(text=text, reply_uid=None)
                except Exception as exc:
                    self.logger.warning("Error processing DM_INTERACTION: %s", exc)
            return

        self.logger.debug("unhandled event: type=%s", event_type)

    async def _handle_pk_start(self, event: dict[str, Any]) -> None:
        if not self.config.features.pk_report_enabled:
            return
        try:
            raw = event.get("data", {})
            # event.data.data 才是 B 站 PK 原始数据
            inner = raw.get("data") if isinstance(raw.get("data"), dict) else raw

            # PK_BATTLE_PRE：对手信息在 inner 顶层
            opp_name = str(inner.get("uname", "") or "")
            opp_uid = int(inner.get("uid", 0) or 0)
            if not opp_name:
                # PK_BATTLE_START_NEW：尝试 match_info.init_info
                match_info = inner.get("match_info", {}) or {}
                init_info = match_info.get("init_info", {}) or {}
                opp_name = str(init_info.get("anchor_name", "") or init_info.get("uname", "") or "")
                if not opp_name:
                    init_info = inner.get("init_info", {}) or {}
                    opp_name = str(init_info.get("anchor_name", "") or init_info.get("uname", "") or "对面主播")
                opp_uid = int(init_info.get("uid", 0) or 0)

            if not opp_name:
                opp_name = "对面主播"

            # ── 通过对手 uid 查信息 ──
            fans = online = guard_count = total_score = 0
            if opp_uid > 0:
                try:
                    opp_user = user.User(uid=opp_uid, credential=self.credential)
                    rel = await asyncio.wait_for(
                        opp_user.get_relation_info(), timeout=5
                    )
                    fans = int(rel.get("follower", 0) or 0)
                    uinfo = await asyncio.wait_for(
                        opp_user.get_user_info(), timeout=5
                    )
                    room_id = (uinfo.get("live_room", {}) or {}).get("roomid")
                    if room_id:
                        opp_room = live.LiveRoom(room_display_id=room_id, credential=self.credential)
                        try:
                            dh = await asyncio.wait_for(
                                opp_room.get_dahanghai(page=1), timeout=5
                            )
                            guard_count = int(dh.get("info", {}).get("num", 0) or 0)
                        except Exception:
                            pass
                        try:
                            gnb = await asyncio.wait_for(
                                opp_room.get_gaonengbang(page=1), timeout=5
                            )
                            online = int(gnb.get("onlineNum", 0) or 0)
                            items = gnb.get("OnlineRankItem", []) or []
                            total_score = sum(int(item.get("score", 0) or 0) for item in items)
                        except Exception:
                            pass
                except asyncio.TimeoutError:
                    self.logger.warning("PK: opponent info query timeout")
                except Exception as exc:
                    self.logger.warning("PK: failed to get opponent info: %s", exc)

            def _fmt(n: int) -> str:
                if n >= 10000:
                    return f"{n / 10000:.1f}万"
                return str(n)

            # 拆成三条弹幕发送
            line1 = f"PK开始！对手{opp_name}"
            line2 = f"对手 {_fmt(fans)}粉，{guard_count}舰"
            line3 = f"{online}观众，贡献{total_score}"
            await self._enqueue_message(text=line1, reply_uid=None)
            await self._enqueue_message(text=line2, reply_uid=None)
            await self._enqueue_message(text=line3, reply_uid=None)
        except Exception as exc:
            self.logger.error("Error processing PK_BATTLE_START: %s", exc, exc_info=True)

    async def _handle_pk_end(self, event: dict[str, Any]) -> None:
        if not self.config.features.pk_end_report_enabled:
            return
        try:
            raw = event.get("data", {})
            # event.data.data 才是 B 站 PK 原始数据
            data = raw.get("data") if isinstance(raw.get("data"), dict) else raw

            self.logger.info("PK end data: %s", json.dumps(data, ensure_ascii=False, default=str)[:1000])

            # _NEW 格式：结果在 init_info.result_type（1=对方胜 2=我方胜）
            pk_init = data.get("init_info", {}) or {}
            result_type = int(pk_init.get("result_type", 0) or 0)
            if result_type == 1:
                win_str = "对方获胜"
            elif result_type == 2:
                win_str = "胜利！"
            else:
                win_str = "PK结束"

            # 分数：init_info.votes 是胜者得分
            my_score = int(pk_init.get("votes", 0) or 0)

            msg = self.config.features.pk_end_template
            msg = msg.replace("{result}", win_str)
            msg = msg.replace("{score}", str(my_score) if my_score else "")
            msg = msg.strip()
            if not msg:
                msg = f"PK {win_str} 我方{my_score}"

            await self._enqueue_message(text=msg, reply_uid=None)
        except Exception as exc:
            self.logger.error("Error processing PK_BATTLE_SETTLE: %s", exc)

    async def _on_connected(self, event: dict[str, Any]) -> None:
        await self._enqueue_message(text=self.config.features.connected_message, reply_uid=None)

    def _has_control_permission(self, uid: int, moderator_hint: bool) -> bool:
        if uid == self.config.anchor_uid:
            return True
        if not self.config.features.allow_admin_as_anchor:
            return False
        if uid in self._admin_uids:
            return True
        return moderator_hint

    def _apply_keyword_filter(self, text: str) -> str | None:
        """Apply keyword filter rules to text. Returns filtered text or None if blocked."""
        kf = self.config.features.keyword_filter
        if not kf:
            return text
        now_h = _now().hour
        for rule in kf:
            kw = rule.get("keyword", "")
            if not kw:
                continue
            # ── 时间段检查 ──
            ts = rule.get("time_start", 0)
            te = rule.get("time_end", 23)
            if not (ts == 0 and te == 23):
                if ts <= te:
                    if not (ts <= now_h <= te):
                        continue
                else:  # 跨天
                    if not (now_h >= ts or now_h <= te):
                        continue
            mode = rule.get("match_mode", "contains")
            action = rule.get("action", "block")
            if mode == "exact":
                matched = text.strip() == kw.strip()
            else:
                matched = kw in text
            if matched:
                if action == "censor":
                    text = text.replace(kw, "*" * len(kw))
                    self.logger.debug("keyword_filter censored: kw=%s text_after=%s", kw, text)
                else:
                    self.logger.debug("keyword_filter blocked: kw=%s text=%s", kw, text)
                    return None
        return text

    async def _enqueue_message(self, text: str, reply_uid: Optional[int]) -> None:
        # 全局数字转大写
        if self.config.features.use_chinese_numbers_global:
            text = _to_chinese_num(text)
        # 去重按 (内容, 收件人)：同一个人重复收到同一句才跳过，
        # 不同人收到同一句是正常的（见 _pending_texts 的注释）
        key = (text, reply_uid)
        if key in self._pending_texts:
            self.logger.debug("dedup enqueue: uid=%s text=%s", reply_uid, text)
            return
        self._pending_texts.add(key)
        try:
            self._msg_queue.put_nowait(OutboundMessage(text=text, reply_uid=reply_uid))
        except asyncio.QueueFull:
            self._pending_texts.discard(key)
            self.logger.warning("queue full, cannot enqueue: reply_uid=%s text=%s", reply_uid, text)

    async def _enqueue_reply(self, text: str, reply_uid: Optional[int]) -> None:
        """Enqueue a command reply with a configurable delay before queueing.
        This gives B站 time to process the previous message before the next one
        enters the outbound queue, reducing the chance of server-side rate limiting.
        
        Dedup is done here (before the delay) so duplicate events from bilibili
        WebSocket reconnection don't cause multiple identical replies.
        """
        # 全局数字转大写
        if self.config.features.use_chinese_numbers_global:
            text = _to_chinese_num(text)

        # Dedup immediately — before the delay window
        key = (text, reply_uid)
        if key in self._pending_texts:
            self.logger.debug("dedup _enqueue_reply: uid=%s text=%s", reply_uid, text)
            return
        self._pending_texts.add(key)

        # 去重键必须**要么交给 _message_worker 清、要么自己还回去**。
        # 中间那段 `await asyncio.sleep(delay)` 是可被取消的：任务一旦在这期间
        # 被取消（或 put_nowait 抛出 QueueFull 之外的异常），键就永远留在
        # `_pending_texts` 里 —— 那句话（同一个人同一内容）以后**再也发不出去**，
        # 而界面上毫无异常。用 finally 兜住所有非成功路径。
        delay = getattr(self.config.rate_limit, 'reply_delay_seconds', 3.0)
        enqueued = False
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            self._msg_queue.put_nowait(OutboundMessage(text=text, reply_uid=reply_uid))
            enqueued = True
        except asyncio.QueueFull:
            self.logger.warning("queue full, cannot enqueue: reply_uid=%s text=%s", reply_uid, text)
        finally:
            if not enqueued:
                self._pending_texts.discard(key)

    def _get_guard_level(self, event: dict[str, Any]) -> int:
        """Extract guard level from welcome event. 0=none, 3=captain, 2=commander, 1=governor."""
        event_type = event.get("type", "")
        data = event.get("data", {})
        if not isinstance(data, dict):
            return 0

        if event_type == "WELCOME_GUARD":
            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            gl = _safe_int(data.get("guard_level") or nested.get("guard_level") or 0)
            if gl in (1, 2, 3):
                return gl
            return 3  # WELCOME_GUARD 事件本身就代表大航海用户

        if event_type == "INTERACT_WORD_V2":
            # INTERACT_WORD_V2 的 privilege_type: 0=普通, 1=总督, 2=提督, 3=舰长
            # bilibili_api 将原始 protobuf 解码到 pb_decoded 字段中
            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            pt = _safe_int(data.get("privilege_type") or nested.get("privilege_type") or 0)
            # 若未在顶层找到，尝试从 pb_decoded 中读取
            if pt == 0:
                pb = nested.get("pb_decoded") if isinstance(nested.get("pb_decoded"), dict) else {}
                pt = _safe_int(pb.get("privilege_type") or 0)
            if pt in (1, 2, 3):
                return pt

        return 0

    def _get_honor_level(self, event: dict[str, Any]) -> int:
        """从进房事件中提取用户的粉丝勋章等级（作为荣耀等级）。"""
        data = event.get("data", {})
        if isinstance(data, dict):
            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            pb = nested.get("pb_decoded") if isinstance(nested.get("pb_decoded"), dict) else {}
            if pb:
                # pb_decoded 中的 fans_medal 或 medal_info
                medal = pb.get("fans_medal") or pb.get("medal_info") or {}
                if isinstance(medal, dict):
                    return _safe_int(medal.get("medal_level") or medal.get("level") or 0)
            # 兜底：从 data 顶层找
            for src in (data, nested):
                medal = src.get("fans_medal") or src.get("medal_info") or {}
                if isinstance(medal, dict):
                    lv = _safe_int(medal.get("medal_level") or medal.get("level") or 0)
                    if lv:
                        return lv
        return 0

    def _record_chat_context(self, text: str, uname: str, uid: int) -> None:
        """记录弹幕到上下文缓存（当配置为 all 时）. """
        from app.web.server import get_llm_config
        cfg = get_llm_config()
        ctx_cfg = cfg.get("context", {})
        if not ctx_cfg.get("enabled", True):
            return
        if ctx_cfg.get("content", "llm_only") != "all":
            return
        ctx_key = 0 if ctx_cfg.get("mode", "isolated") == "merged" else uid
        # 上限必须夹住：ctx_max=0 时 `history[-0:]` 等于 `history[0:]`（整表返回），
        # 列表会越滚越大且永不收敛 —— 实测 50 条 → 55 条 → 一直涨。
        # 负数更糟（`history[5:]` 只裁头不裁尾）。配置写错不能让内存无限涨。
        try:
            ctx_max = int(ctx_cfg.get("max_messages", 10))
        except (TypeError, ValueError):
            ctx_max = 10
        ctx_max = max(1, min(ctx_max, _MAX_CONTEXT_MESSAGES))

        history = self._chat_contexts.get(ctx_key)
        if history is None:
            history = []
        history.append({"role": "user", "content": f'用户"{uname}"说: {text}'})
        if len(history) > ctx_max:
            history = history[-ctx_max:]
        self._chat_contexts[ctx_key] = history
        self._chat_contexts.move_to_end(ctx_key)
        # 长时间运行 + 大量不同观众时，这个表会一直涨（每人一份历史）。
        # 超过上限就按「最久没说话的人」淘汰 —— 他被淘汰后下次说话会重新开始记，
        # 对 AI 回复质量影响很小，但内存有确定上限。
        while len(self._chat_contexts) > _MAX_CHAT_CONTEXTS:
            self._chat_contexts.popitem(last=False)


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return 0


def _to_chinese_num(text: str) -> str:
    """将字符串中的阿拉伯数字替换为中文大写数字，避免 B站 数字拦截."""
    mapping = {"0": "〇", "1": "壹", "2": "贰", "3": "叁", "4": "肆",
               "5": "伍", "6": "陆", "7": "柒", "8": "捌", "9": "玖"}
    return "".join(mapping.get(ch, ch) for ch in text)


def _extract_live_status(data: Any) -> bool:
    if isinstance(data, dict):
        for key in ("live_status", "liveStatus", "status"):
            if key in data and _safe_int(data[key]) == 1:
                return True
        for value in data.values():
            if _extract_live_status(value):
                return True
    elif isinstance(data, list):
        for value in data:
            if _extract_live_status(value):
                return True
    return False


def _extract_enter_uid_uname(event: dict[str, Any]) -> tuple[int, str]:
    data = event.get("data", {})

    if isinstance(data, dict):
        pb = data.get("data", {}).get("pb_decoded") if isinstance(data.get("data"), dict) else None
        if isinstance(pb, dict):
            uid = _safe_int(pb.get("uid"))
            uname = str(pb.get("uname", ""))
            return uid, uname

        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        uid = _safe_int(data.get("uid") or nested.get("uid") or 0)
        uname = data.get("uname")
        if not uname and isinstance(nested, dict):
            uname = nested.get("uname")
        return uid, str(uname or "")

    return 0, ""


# ── 模块级时区，由 LiveRobot 初始化时根据 config 设置 ──
_TZ = ZoneInfo("Asia/Shanghai") if ZoneInfo else None  # type: ignore[misc]


def set_bot_timezone(tz_name: str) -> None:
    """设置 Bot 运行时使用的时区（从 config.runtime.timezone 读取）。

    同时同步给 `app.tzutil`：统计/导出/删除那些按日期算的地方都用它，
    三套时间算法不一致会让跨天数据错位（见 tzutil 模块开头的说明）。
    """
    global _TZ
    if ZoneInfo:
        try:
            _TZ = ZoneInfo(tz_name)
        except Exception:
            _TZ = None
    try:
        from app import tzutil
        tzutil.set_config_timezone(tz_name)
    except Exception:
        pass


def _now() -> datetime:
    """返回配置时区的当前时间。

    直接走 `app.tzutil`，让「机器人写 month/开播日期」和「WebUI 按日期筛选/导出/删除」
    共用同一个时区来源 —— 以前这里是独立的一套 `_TZ`，两套口径会悄悄漂移。
    """
    from app import tzutil
    return tzutil.now()


def _is_in_time_slot(time_start: int, time_end: int, current_hour: int | None = None) -> bool:
    """检查当前小时是否在 [time_start, time_end] 时段内。
    支持跨天：time_start=22, time_end=6 → 22:00~06:00 都满足。
    """
    if current_hour is None:
        current_hour = _now().hour
    if time_start <= time_end:
        return time_start <= current_hour <= time_end
    # 跨天: 22~6 → 22<=h<=23 或 0<=h<=6
    return current_hour >= time_start or current_hour <= time_end


def _pick_template_from_list(
    templates_list: list[dict] | None,
    uname: str = "",
    uid: int = 0,
    guard_level: int = 0,
    logger: Any = None,
) -> str | None:
    """从模板列表中随机选择一条当前时段生效的模板。"""
    if not templates_list:
        return None
    # 过滤当前时段生效的模板
    valid = []
    for t in templates_list:
        ts = int(t.get("time_start", 0))
        te = int(t.get("time_end", 23))
        if _is_in_time_slot(ts, te):
            valid.append(t.get("text", ""))
    if not valid:
        if logger:
            logger.debug("no template in current time slot (list has %d entries)", len(templates_list))
        return None
    # 如果有时段匹配但 text 为空，尝试用第一条
    text = random.choice(valid)
    if not text and templates_list:
        text = templates_list[0].get("text", "")
    return text if text else None


def _extract_gift_payload(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    data = event.get("data", {})
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        # Try flat structure (some event types put gift data at data root)
        payload = data if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return None

    uid = _safe_int(payload.get("uid"))
    uname = str(payload.get("uname") or "")
    if not uname:
        # 有些事件把昵称放在 sender_uinfo / user_info 里。取不到就留空，
        # **不要因此丢掉整条礼物** —— 以前 `if uid <= 0 or not uname: return None`
        # 会让「少一个昵称字段」变成「整场直播的统计都是 0」，且一条日志都没有。
        for holder_key in ("sender_uinfo", "user_info", "user", "uinfo"):
            holder = payload.get(holder_key)
            if isinstance(holder, dict):
                uname = str(holder.get("uname") or holder.get("name") or "")
                if not uname:
                    base = holder.get("base")
                    if isinstance(base, dict):
                        uname = str(base.get("name") or base.get("uname") or "")
                if uname:
                    break
    gift_name = str(payload.get("giftName") or payload.get("gift_name") or "gift")
    gift_num = _safe_int(payload.get("num") or payload.get("gift_num") or 1)
    event_type = str(data.get("cmd") or event.get("type") or "SEND_GIFT")

    if uid <= 0:
        return None

    return {
        "uid": uid,
        "uname": uname,
        "gift_name": gift_name,
        "gift_num": max(gift_num, 1),
        "event_type": event_type,
        "raw": payload,
    }


#: 已知的、我们**会处理**的礼物类事件名。
KNOWN_GIFT_EVENTS = frozenset({
    "SEND_GIFT", "SEND_GIFT_V2", "COMBO_SEND",
    "UNIVERSAL_EVENT_GIFT", "UNIVERSAL_EVENT_GIFT_V2", "SPECIAL_GIFT",
})
#: 名字里带这些词就当成「礼物类」事件 —— 用来发现我们没接的新协议名。
#: B站 的命名习惯基本都能命中（SEND_GIFT / UNIVERSAL_EVENT_GIFT / GIFT_STAR_PROCESS…）。
_GIFT_LIKE_HINTS = ("GIFT", "COMBO")


def _looks_like_gift_event(name: str) -> bool:
    up = (name or "").upper()
    return any(h in up for h in _GIFT_LIKE_HINTS)


GUARD_NAMES = {1: "总督", 2: "提督", 3: "舰长"}
GUARD_TYPE_KEYS = {1: "governor", 2: "commander", 3: "captain"}


def _extract_guard_buy_payload(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """解析上舰事件。

    B站 同一次开通会连续推送三种事件，字段结构各不相同：
      - GUARD_BUY        : data.data = {uid, username, guard_level, num, price, start_time}
      - USER_TOAST_MSG   : data.data = 同上 + role_name / toast_msg
      - USER_TOAST_MSG_V2: data.data = {sender_uinfo, guard_info, pay_info, ...}

    历史 bug（2026-04-28 引入）：uid/uname 用了
    `payload.get("uid") or payload.get("user_info", {}).get("uid") if isinstance(user_info, dict) else 0`
    这种写法，条件表达式优先级导致「没有 user_info 字段时整个表达式取 0」，
    而三种真实事件都没有 user_info → 永远解析失败 → 上舰文案与记录全部丢失。
    """
    data = event.get("data", {})
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        payload = data if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return None

    uid = 0
    uname = ""
    guard_level = 0
    months = 1
    price_coin = 0
    start_time = 0

    # 形态 A：USER_TOAST_MSG_V2（嵌套 sender_uinfo / guard_info / pay_info）
    sender = payload.get("sender_uinfo")
    if isinstance(sender, dict):
        uid = _safe_int(sender.get("uid"))
        base = sender.get("base")
        if not uid and isinstance(base, dict):
            uid = _safe_int(base.get("uid"))
        if isinstance(base, dict):
            uname = str(base.get("name") or "")
        guard_info = payload.get("guard_info")
        if isinstance(guard_info, dict):
            guard_level = _safe_int(guard_info.get("guard_level"))
            start_time = _safe_int(guard_info.get("start_time"))
        pay_info = payload.get("pay_info")
        if isinstance(pay_info, dict):
            months = _safe_int(pay_info.get("num")) or 1
            price_coin = _safe_int(pay_info.get("price"))

    # 形态 B：GUARD_BUY / USER_TOAST_MSG（扁平结构）
    if uid <= 0:
        uid = _safe_int(payload.get("uid"))
    if not uname:
        uname = str(payload.get("username") or payload.get("uname") or "")
    if guard_level <= 0:
        guard_level = _safe_int(payload.get("guard_level") or payload.get("level"))
    if start_time <= 0:
        start_time = _safe_int(payload.get("start_time"))
    if price_coin <= 0:
        price_coin = _safe_int(payload.get("price"))
    if months <= 1:
        months = max(_safe_int(payload.get("num") or payload.get("month")) or months, 1)

    # 形态 C：旧版 user_info 包裹（历史兼容）
    user_info = payload.get("user_info")
    if isinstance(user_info, dict):
        if uid <= 0:
            uid = _safe_int(user_info.get("uid"))
        if not uname:
            uname = str(user_info.get("uname") or user_info.get("name") or "")

    if uid <= 0 or not uname:
        return None

    guard_name = GUARD_NAMES.get(guard_level, str(payload.get("role_name") or payload.get("gift_name") or "大航海"))
    if not guard_name or guard_name == "None":
        guard_name = "大航海"

    now_ts = int(time.time())
    return {
        "uid": uid,
        "uname": uname,
        "months": max(months, 1),
        "guard_level": guard_level,
        "guard_type": GUARD_TYPE_KEYS.get(guard_level, "guard"),
        "guard_name": guard_name,
        "price_coin": price_coin,
        # 金瓜子 → 电池（与礼物统计口径一致：÷100）
        "value": price_coin // 100 if price_coin > 0 else 0,
        "start_time": start_time or now_ts,
        "source": str(event.get("type") or ""),
    }


def _extract_blindbox_profit(payload: dict[str, Any]) -> dict[str, Any]:
    gift_name = str(payload.get("giftName") or payload.get("gift_name") or "")
    blind_raw = payload.get("blind_gift")
    blind_dict = blind_raw if isinstance(blind_raw, dict) else None

    # Check multiple possible blindbox indicator fields
    is_blind_box_flag = payload.get("is_blind_box")
    blind_box_flag = payload.get("blind_box")

    def _is_truthy(val: Any) -> bool:
        if isinstance(val, bool):
            return val
        if isinstance(val, dict):
            return len(val) > 0  # non-empty dict = blindbox metadata
        n = _safe_int(val)
        return n == 1

    is_blind = (
        _is_truthy(blind_raw)
        or _is_truthy(is_blind_box_flag)
        or _is_truthy(blind_box_flag)
        or ("盲盒" in gift_name)
    )

    if not is_blind:
        return {
            "is_blind_box": False,
            "blind_box_cost": 0,
            "actual_value": 0,
            "profit_value": 0,
        }

    num = max(_safe_int(payload.get("num") or payload.get("gift_num") or 1), 1)

    # Blindbox unit cost (API returns values in 分/金瓜子, convert to 电池 by ÷100)
    blind_unit = 0
    if blind_dict:
        blind_unit = _safe_int(blind_dict.get("original_gift_price"))
    if blind_unit == 0:
        for key in ("blind_gift_price", "blind_price", "box_price"):
            if key in payload and _safe_int(payload.get(key)) > 0:
                blind_unit = _safe_int(payload.get(key))
                break
    blind_unit //= 100

    # Actual revealed gift value: for blindboxes, blind_gift.gift_tip_price is
    # the revealed item's worth per item, NOT total_coin (which is what the user paid).
    actual_total = 0
    if blind_dict:
        actual_total = _safe_int(blind_dict.get("gift_tip_price")) * num
    if actual_total == 0:
        for key in ("total_coin", "original_gift_price_total", "gift_price_total"):
            if key in payload and _safe_int(payload.get(key)) > 0:
                actual_total = _safe_int(payload.get(key))
                break
    if actual_total == 0:
        for key in ("original_gift_price", "original_price", "gift_price"):
            if key in payload and _safe_int(payload.get(key)) > 0:
                actual_total = _safe_int(payload.get(key)) * num
                break
    actual_total //= 100

    blind_total = blind_unit * num

    return {
        "is_blind_box": True,
        "blind_box_cost": blind_total,
        "actual_value": actual_total,
        "profit_value": actual_total - blind_total,
    }


def _parse_danmaku_user_and_text(event: dict[str, Any]) -> Optional[tuple[int, str, str, bool]]:
    data = event.get("data", {})
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, list) or len(info) < 3:
        return None

    text = str(info[1]) if len(info) > 1 else ""
    user_info = info[2]
    if not isinstance(user_info, list) or not user_info:
        return None

    uid = _safe_int(user_info[0])
    uname = str(user_info[1]) if len(user_info) > 1 else ""

    moderator_hint = False
    if len(user_info) >= 3:
        moderator_hint = _safe_int(user_info[2]) in (1, 2, 3)

    return uid, uname, text.strip(), moderator_hint


def _parse_command(text: str, allow_bare: bool = False) -> Optional[tuple[str, str]]:
    s = text.strip()
    # Normalize full-width '#'
    if s.startswith("＃"):
        s = "#" + s[1:]

    had_hash = s.startswith("#")

    # Try matching with the # prefix
    result = _match_hash_command(s)
    if result is not None:
        return result

    # Bare mode: also try with # prepended (only if original text didn't have #)
    if allow_bare and not had_hash:
        return _match_hash_command("#" + s)

    return None


def _match_hash_command(s: str) -> Optional[tuple[str, str]]:
    """匹配 # 开头的指令模式。s 已经是规范化后的字符串。"""
    if not s.startswith("#"):
        return None

    compact = "".join(s.split()).replace("：", ":")

    # Command: #{wake_word} (e.g. #文文)
    wake = _CURRENT_WAKE_WORD
    wake_hash = f"#{wake}"
    if compact == wake_hash or compact.startswith(wake_hash):
        rest = ""
        if compact == wake_hash:
            pass
        elif compact.startswith(f"{wake_hash}:"):
            rest = compact[len(f"{wake_hash}:"):]
        elif compact.startswith(wake_hash):
            rest = compact[len(wake_hash):]
        if not rest and (s.startswith(f"{wake_hash} ") or s.startswith(f"＃{wake} ")):
            rest = s.split(" ", 1)[1] if " " in s else ""
        if not rest and "：" in s:
            parts = s.split("：", 1)
            if parts[0].strip() in (wake_hash, f"＃{wake}"):
                rest = parts[1].strip()
        if not rest:
            return "llm_chat", ""
        return "llm_chat", rest.strip()

    if compact in ("#帮助", "#help"):
        return "help", ""
    if compact == "#签到":
        return "checkin", ""
    if compact == "#抽签":
        return "fortune", ""
    if compact in ("#盲盒统计", "#盲盒我的"):
        return "blindbox_me", ""
    if compact.startswith("#盲盒统计:"):
        return "blindbox_me", compact[len("#盲盒统计:"):]
    if s.startswith("#盲盒统计 ") or s.startswith("＃盲盒统计 "):
        return "blindbox_me", s[len("#盲盒统计 "):].strip()
    if compact == "#今日盲盒":
        return "today_blindbox", ""
    if compact.startswith("#今日盲盒:"):
        return "today_blindbox", compact[len("#今日盲盒:"):]
    if s.startswith("#今日盲盒 ") or s.startswith("＃今日盲盒 "):
        return "today_blindbox", s[len("#今日盲盒 "):].strip()
    if compact == "#本月盲盒":
        return "blindbox_me", ""
    if compact.startswith("#本月盲盒:"):
        return "blindbox_me", compact[len("#本月盲盒:"):]
    if s.startswith("#本月盲盒 ") or s.startswith("＃本月盲盒 "):
        return "blindbox_me", s[len("#本月盲盒 "):].strip()
    if compact in ("#欢迎:开", "＃欢迎:开"):
        return "welcome_on", ""
    if compact in ("#欢迎:关", "＃欢迎:关"):
        return "welcome_off", ""
    if s == "#欢迎 开" or compact == "#欢迎开":
        return "welcome_on", ""
    if s == "#欢迎 关" or compact == "#欢迎关":
        return "welcome_off", ""
    
    if s.startswith("#欢迎 词 ") or s.startswith("#欢迎词 "):
        prefix = "#欢迎 词 " if s.startswith("#欢迎 词 ") else "#欢迎词 "
        return "welcome_text", s[len(prefix) :].strip()

    # Backward compatibility for old English commands
    if s == "#welcome on" or compact == "#welcomeon":
        return "welcome_on", ""
    if s == "#welcome off" or compact == "#welcomeoff":
        return "welcome_off", ""
    if s.startswith("#welcome text "):
        return "welcome_text", s[len("#welcome text ") :].strip()

    return None


# ══════════════════════════════════════════════════════════════
#  可配置唤醒词
# ══════════════════════════════════════════════════════════════

_CURRENT_WAKE_WORD = "ayabot"


def _set_wake_word(word: str) -> None:
    global _CURRENT_WAKE_WORD
    _CURRENT_WAKE_WORD = word


def _match_keyword_rule(text: str, rules: list[KeywordRule], uid: int = 0) -> Optional[str]:
    for rule in rules:
        if rule.allowed_uids and uid not in rule.allowed_uids:
            continue
        # 检查时段
        if not _is_in_time_slot(rule.time_start, rule.time_end):
            continue
        for keyword in rule.keywords:
            if rule.match_mode == "exact":
                if text == keyword:
                    return rule.reply
            elif rule.match_mode == "startswith":
                if text.startswith(keyword):
                    return rule.reply
            else:  # "contains" (default)
                if keyword in text:
                    return rule.reply
    return None
