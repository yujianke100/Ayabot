from __future__ import annotations

import asyncio
import re
from contextlib import suppress
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Python < 3.9 fallback
    ZoneInfo = None  # type: ignore[assignment,misc]

from bilibili_api import Credential, live, user
from bilibili_api.utils.danmaku import Danmaku

from .config import AppConfig, KeywordRule
from .storage import GiftEvent, StatsStore


@dataclass(slots=True)
class OutboundMessage:
    text: str
    reply_uid: Optional[int]
    retry_count: int = 0


class LiveRobot:
    def __init__(self, config: AppConfig, credential: Optional[Credential] = None) -> None:
        self.config = config
        self.logger = logging.getLogger("bili-live-robot")

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

        max_queue = getattr(config.rate_limit, 'max_queue_size', 50)
        self._msg_queue: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=max_queue)
        self._msg_worker_task: Optional[asyncio.Task[None]] = None

        self._danmaku: Optional[live.LiveDanmaku] = None
        self._danmaku_task: Optional[asyncio.Task[None]] = None

        self._last_welcome_ts: dict[int, float] = {}
        self._last_thanks_ts: dict[int, float] = {}

        self._pending_texts: set[str] = set()
        self._chat_contexts: dict[int, list[dict[str, str]]] = {}

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
        self.logger.info("robot started, room=%s", self.config.room_display_id)
        self._poll_counter = 0
        self._msg_worker_task = asyncio.create_task(self._message_worker())
        self._periodic_task = asyncio.create_task(self._periodic_message_loop())

        try:
            while True:
                is_live = await self._is_room_live()
                if is_live and self._danmaku is None:
                    await self._start_danmaku()
                if (not is_live) and self._danmaku is not None:
                    await self._stop_danmaku()

                self._poll_counter = (self._poll_counter + 1) % 10
                if self._poll_counter == 0:
                    self.logger.info("poll: live=%s danmaku=%s", is_live, self._danmaku is not None)
                await asyncio.sleep(self.config.runtime.poll_interval_seconds)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        await self._stop_danmaku()
        if self._msg_worker_task:
            self._msg_worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._msg_worker_task
        if self._periodic_task:
            self._periodic_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._periodic_task
        self._in_pk = False
        self.store.close()

    async def _is_room_live(self) -> bool:
        try:
            info = await self.live_room.get_room_play_info()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("poll room status failed: %s", exc)
            return False

        return _extract_live_status(info)

    async def _start_danmaku(self) -> None:
        self._danmaku = live.LiveDanmaku(
            room_display_id=self.config.room_display_id,
            credential=self.credential,
        )

        self._danmaku.on("INTERACT_WORD_V2")(self._on_enter_room)

        self._danmaku.on("SEND_GIFT")(self._on_gift)
        self._danmaku.on("COMBO_SEND")(self._on_gift)
        self._danmaku.on("UNIVERSAL_EVENT_GIFT")(self._on_gift)
        self._danmaku.on("UNIVERSAL_EVENT_GIFT_V2")(self._on_gift)
        self._danmaku.on("SPECIAL_GIFT")(self._on_special_gift)
        self._danmaku.on("GUARD_BUY")(self._on_guard_buy)
        self._danmaku.on("USER_TOAST_MSG")(self._on_guard_buy)
        self._danmaku.on("USER_TOAST_MSG_V2")(self._on_guard_buy)

        self._danmaku.on("LIKE_INFO_V3_CLICK")(self._on_like)
        self._danmaku.on("ROOM_ADMINS")(self._on_room_admins)
        self._danmaku.on("DANMU_MSG")(self._on_danmaku)

        # Log all events for debugging blindbox and unknown event types
        self._danmaku.on("ALL")(self._on_all_events)

        # Send connected message when WebSocket auth succeeds
        if self.config.features.connected_message_enabled and self.config.features.connected_message:
            self._danmaku.on("VERIFICATION_SUCCESSFUL")(self._on_connected)

        async def _run_connect() -> None:
            assert self._danmaku is not None
            await self._danmaku.connect()

        self._danmaku_task = asyncio.create_task(_run_connect())
        self.logger.info("danmaku connected")
        # 记录开播日期
        self.store.record_stream_date(_now().strftime("%Y-%m-%d"))

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

            self._pending_texts.discard(text)
            self._pending_texts.add(text)
            await self._msg_queue.put(OutboundMessage(text=text, reply_uid=None))

    async def _message_worker(self) -> None:
        min_interval = max(self.config.rate_limit.send_interval_seconds, 0.1)
        max_queue = getattr(self.config.rate_limit, 'max_queue_size', 50)
        max_retries_per_msg = 3
        last_sent = 0.0
        consecutive_fails = 0
        current_interval = min_interval
        log_counter = 0

        while True:
            msg = await self._msg_queue.get()

            # Remove from pending set (no longer pending)
            self._pending_texts.discard(msg.text)

            # Truncate to stay within Bilibili's danmaku length limit (~30 Chinese chars)
            MAX_TEXT_LEN = 30
            if len(msg.text) > MAX_TEXT_LEN:
                self.logger.warning(
                    "message truncated: len=%d > %d text=%s", len(msg.text), MAX_TEXT_LEN, msg.text
                )
                msg.text = msg.text[:MAX_TEXT_LEN]

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

        # ── 检测关注/转发事件（INTERACT_WORD_V2 msg_type） ──
        data = event.get("data", {})
        msg_type = 0
        if isinstance(data, dict):
            nested = data.get("data") if isinstance(data.get("data"), dict) else data
            msg_type = int(nested.get("msg_type", 0))

        if msg_type == 2 and self.config.features.follow_thanks_enabled:
            template = self.config.features.follow_thanks_template
            if template:
                text = template.replace("{uname}", uname)
                await self._enqueue_message(text=text, reply_uid=uid)
            return
        if msg_type == 3 and self.config.features.share_thanks_enabled:
            template = self.config.features.share_thanks_template
            if template:
                text = template.replace("{uname}", uname)
                await self._enqueue_message(text=text, reply_uid=uid)
            return

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
            # 欢迎文本去重：相同的内容不重复入队（与 _enqueue_reply 的去重一致）
            if text in self._pending_texts:
                self.logger.debug("dedup welcome text: uid=%s text=%s", uid, text)
                return
            self._pending_texts.add(text)
            try:
                self._msg_queue.put_nowait(OutboundMessage(text=text, reply_uid=uid))
            except asyncio.QueueFull:
                self._pending_texts.discard(text)
                self.logger.warning("queue full, welcome dropped: uid=%s", uid)

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
        if not self.config.features.thanks_enabled:
            return

        gift = _extract_gift_payload(event)
        if gift is None:
            return

        uid = gift["uid"]
        uname = gift["uname"]
        gift_name = gift["gift_name"]
        gift_num = gift["gift_num"]

        self.logger.debug(
            "gift event: type=%s uid=%s gift_name=%s num=%s raw_keys=%s",
            gift["event_type"], uid, gift_name, gift_num,
            list(gift["raw"].keys()),
        )

        # Record to DB regardless of cooldown — always persist stats
        now = time.time()
        blind = _extract_blindbox_profit(gift["raw"])
        if not blind["is_blind_box"]:
            self.logger.debug(
                "gift not detected as blindbox: uid=%s gift_name=%s raw_keys=%s blind_gift=%s",
                uid, gift_name, list(gift["raw"].keys()),
                gift["raw"].get("blind_gift"),
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
            event_type=gift["event_type"],
            gift_name=gift_name,
            gift_num=gift_num,
            is_blind_box=is_blind,
            blind_box_cost=cost,
            actual_value=actual,
            profit_value=profit,
            raw_json=json.dumps(gift["raw"], ensure_ascii=False),
        )
        self.store.record_gift_event(event_row)

        # Cooldown only gates the thank-you message, not recording
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

    async def _on_special_gift(self, event: dict[str, Any]) -> None:
        self.logger.debug(
            "SPECIAL_GIFT event received: data=%s",
            event.get("data"),
        )

    async def _on_guard_buy(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "?")
        if not self.config.features.guard_thanks_enabled:
            return

        guard = _extract_guard_buy_payload(event)
        if guard is None:
            self.logger.debug("guard_buy parse failed: type=%s data=%s", event_type, event.get("data"))
            return

        uid = guard["uid"]
        uname = guard["uname"]
        months = guard["months"]
        guard_type = guard["guard_type"]

        now = time.time()
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
            .replace("{guard_type}", guard_type)
        )
        await self._enqueue_message(text=text, reply_uid=uid)

    async def _on_danmaku(self, event: dict[str, Any]) -> None:
        parsed = _parse_danmaku_user_and_text(event)
        if parsed is None:
            self.logger.warning("danmaku parse failed: raw_event_type=%s keys=%s", event.get("type"), list(event.get("data", {}).keys() if event.get("data") else []))
            return

        uid, uname, text, moderator_hint = parsed
        self.logger.debug("danmaku received: uid=%s text=%s (anchor_uid=%s)", uid, text, self.config.anchor_uid)

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
            today_start = int(datetime(now.year, now.month, now.day).timestamp())
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
            ctx_max = ctx_cfg.get("max_messages", 10)

            history: list[dict[str, str]] = []
            if ctx_enabled:
                ctx_key = 0 if ctx_mode == "merged" else uid
                history = self._chat_contexts.get(ctx_key, [])
                history = history[-ctx_max:] if len(history) > ctx_max else history

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

    async def _on_all_events(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "?")

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
                       "WELCOME", "WELCOME_GUARD", "DANMU_MSG",
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
                    opp_user = user.User(uid=opp_uid)
                    rel = await asyncio.wait_for(
                        opp_user.get_relation_info(), timeout=5
                    )
                    fans = int(rel.get("follower", 0) or 0)
                    uinfo = await asyncio.wait_for(
                        opp_user.get_user_info(), timeout=5
                    )
                    room_id = (uinfo.get("live_room", {}) or {}).get("roomid")
                    if room_id:
                        opp_room = live.LiveRoom(room_display_id=room_id)
                        try:
                            dh = await asyncio.wait_for(
                                opp_room.get_dahanghai(page=1), timeout=5
                            )
                            guard_count = int(dh.get("info", {}).get("num", 0) or 0)
                        except Exception:
                            pass
                        # 高能榜：在线人数 + 总贡献
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
        if not self.config.features.pk_report_enabled:
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

    async def _enqueue_message(self, text: str, reply_uid: Optional[int]) -> None:
        # Deduplicate: skip if same text is already queued
        if text in self._pending_texts:
            self.logger.debug("dedup enqueue: text=%s", text)
            return
        self._pending_texts.add(text)
        try:
            self._msg_queue.put_nowait(OutboundMessage(text=text, reply_uid=reply_uid))
        except asyncio.QueueFull:
            self._pending_texts.discard(text)
            self.logger.warning("queue full, cannot enqueue: reply_uid=%s text=%s", reply_uid, text)

    async def _enqueue_reply(self, text: str, reply_uid: Optional[int]) -> None:
        """Enqueue a command reply with a configurable delay before queueing.
        This gives B站 time to process the previous message before the next one
        enters the outbound queue, reducing the chance of server-side rate limiting.
        
        Dedup is done here (before the delay) so duplicate events from bilibili
        WebSocket reconnection don't cause multiple identical replies.
        """
        # Dedup immediately — before the delay window
        if text in self._pending_texts:
            self.logger.debug("dedup _enqueue_reply: text=%s", text)
            return
        self._pending_texts.add(text)

        delay = getattr(self.config.rate_limit, 'reply_delay_seconds', 3.0)
        if delay > 0:
            await asyncio.sleep(delay)
        # Directly enqueue (dedup already done). _pending_texts cleared by _message_worker.
        try:
            self._msg_queue.put_nowait(OutboundMessage(text=text, reply_uid=reply_uid))
        except asyncio.QueueFull:
            self._pending_texts.discard(text)
            self.logger.warning("queue full, cannot enqueue: reply_uid=%s text=%s", reply_uid, text)

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
        ctx_max = ctx_cfg.get("max_messages", 10)
        history = self._chat_contexts.get(ctx_key, [])
        history.append({"role": "user", "content": f'用户"{uname}"说: {text}'})
        if len(history) > ctx_max:
            history = history[-ctx_max:]
        self._chat_contexts[ctx_key] = history


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return 0


def _to_chinese_num(text: str) -> str:
    """将字符串中的阿拉伯数字替换为中文数字，避免 B站 数字拦截."""
    mapping = {"0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
               "5": "五", "6": "六", "7": "七", "8": "八", "9": "九"}
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
    """设置 Bot 运行时使用的时区（从 config.runtime.timezone 读取）。"""
    global _TZ
    if ZoneInfo:
        try:
            _TZ = ZoneInfo(tz_name)
        except Exception:
            _TZ = None


def _now() -> datetime:
    """返回配置时区的当前时间（兜底 UTC）。"""
    if _TZ:
        return datetime.now(_TZ)
    return datetime.now()


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
    uname = str(payload.get("uname", ""))
    gift_name = str(payload.get("giftName") or payload.get("gift_name") or "gift")
    gift_num = _safe_int(payload.get("num") or payload.get("gift_num") or 1)
    event_type = str(data.get("cmd") or event.get("type") or "SEND_GIFT")

    if uid <= 0 or not uname:
        return None

    return {
        "uid": uid,
        "uname": uname,
        "gift_name": gift_name,
        "gift_num": max(gift_num, 1),
        "event_type": event_type,
        "raw": payload,
    }


def _extract_guard_buy_payload(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    data = event.get("data", {})
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        payload = data if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return None

    uid = _safe_int(payload.get("uid") or payload.get("user_info", {}).get("uid") if isinstance(payload.get("user_info"), dict) else 0)
    uname = str(payload.get("username") or payload.get("uname") or payload.get("user_info", {}).get("uname") if isinstance(payload.get("user_info"), dict) else "")
    months = max(_safe_int(payload.get("num") or payload.get("month") or 1), 1)
    guard_level = _safe_int(payload.get("guard_level") or payload.get("level") or 0)

    if uid <= 0 or not uname:
        return None

    # Common Bilibili mapping: 1=Governor, 2=Commander, 3=Captain
    if guard_level == 1:
        guard_type = "governor"
    elif guard_level == 2:
        guard_type = "commander"
    elif guard_level == 3:
        guard_type = "captain"
    else:
        guard_type = "guard"

    return {
        "uid": uid,
        "uname": uname,
        "months": months,
        "guard_type": guard_type,
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
    # the revealed item's worth, NOT total_coin (which is what the user paid).
    actual_total = 0
    if blind_dict:
        actual_total = _safe_int(blind_dict.get("gift_tip_price"))
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
