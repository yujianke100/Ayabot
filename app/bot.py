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

from bilibili_api import Credential, live
from bilibili_api.utils.danmaku import Danmaku

from .config import AppConfig, KeywordRule
from .storage import GiftEvent, StatsStore


@dataclass(slots=True)
class OutboundMessage:
    text: str
    reply_uid: Optional[int]


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

        self._msg_queue: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._msg_worker_task: Optional[asyncio.Task[None]] = None

        self._danmaku: Optional[live.LiveDanmaku] = None
        self._danmaku_task: Optional[asyncio.Task[None]] = None

        self._welcome_enabled = config.features.welcome_enabled
        self._welcome_template = config.features.welcome_template
        self._thanks_template = config.features.thanks_template

        self._last_welcome_ts: dict[int, float] = {}
        self._last_thanks_ts: dict[int, float] = {}

        self._admin_uids: set[int] = set()
        self._keyword_reply_cooldown_ts: dict[int, float] = {}

    async def run(self) -> None:
        self.logger.info("robot started, room=%s", self.config.room_display_id)
        self._msg_worker_task = asyncio.create_task(self._message_worker())

        try:
            while True:
                is_live = await self._is_room_live()
                if is_live and self._danmaku is None:
                    await self._start_danmaku()
                if (not is_live) and self._danmaku is not None:
                    await self._stop_danmaku()

                await asyncio.sleep(self.config.runtime.poll_interval_seconds)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        await self._stop_danmaku()
        if self._msg_worker_task:
            self._msg_worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._msg_worker_task
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
        self._danmaku.on("WELCOME")(self._on_enter_room)
        self._danmaku.on("WELCOME_GUARD")(self._on_enter_room)

        self._danmaku.on("SEND_GIFT")(self._on_gift)
        self._danmaku.on("COMBO_SEND")(self._on_gift)
        self._danmaku.on("UNIVERSAL_EVENT_GIFT")(self._on_gift)
        self._danmaku.on("UNIVERSAL_EVENT_GIFT_V2")(self._on_gift)
        self._danmaku.on("SPECIAL_GIFT")(self._on_special_gift)
        self._danmaku.on("GUARD_BUY")(self._on_guard_buy)
        self._danmaku.on("USER_TOAST_MSG")(self._on_guard_buy)
        self._danmaku.on("USER_TOAST_MSG_V2")(self._on_guard_buy)

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

    async def _message_worker(self) -> None:
        min_interval = max(self.config.rate_limit.send_interval_seconds, 0.1)
        last_sent = 0.0

        while True:
            msg = await self._msg_queue.get()

            # Truncate to stay within Bilibili's danmaku length limit (~30 Chinese chars)
            MAX_TEXT_LEN = 30
            if len(msg.text) > MAX_TEXT_LEN:
                self.logger.warning(
                    "message truncated: len=%d > %d text=%s", len(msg.text), MAX_TEXT_LEN, msg.text
                )
                msg.text = msg.text[:MAX_TEXT_LEN]

            self.logger.debug("message dequeued: reply_uid=%s text=%s", msg.reply_uid, msg.text)
            wait_s = min_interval - (time.time() - last_sent)
            if wait_s > 0:
                await asyncio.sleep(wait_s)

            try:
                danmaku = Danmaku(text=msg.text)
                retries = self.config.rate_limit.retry_count
                for attempt in range(1 + retries):
                    try:
                        resp = await self.live_room.send_danmaku(danmaku=danmaku, reply_mid=msg.reply_uid)
                    except Exception as exc:  # noqa: BLE001
                        if attempt < retries:
                            self.logger.debug(
                                "send danmaku retry %d/%d: reply_uid=%s err=%s",
                                attempt + 1, retries, msg.reply_uid, exc,
                            )
                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                        self.logger.warning(
                            "send danmaku failed after %d retries: reply_uid=%s text=%s err=%s",
                            retries, msg.reply_uid, msg.text, exc,
                        )
                        break
                    else:
                        last_sent = time.time()
                        self.logger.debug(
                            "send danmaku success: reply_uid=%s text=%s resp=%s",
                            msg.reply_uid, msg.text, resp,
                        )
                        break
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "send danmaku unexpected error: reply_uid=%s err=%s",
                    msg.reply_uid, exc,
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
        if not self._welcome_enabled:
            return

        uid, uname = _extract_enter_uid_uname(event)
        if uid <= 0 or not uname:
            return

        now = time.time()
        last = self._last_welcome_ts.get(uid, 0.0)
        if now - last < self.config.cooldown.welcome_user_seconds:
            return

        self._last_welcome_ts[uid] = now
        text = self._welcome_template.replace("{uname}", uname)
        await self._enqueue_message(text=text, reply_uid=uid)

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

        month = datetime.now().strftime("%Y-%m")
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
            self._thanks_template.replace("{uname}", uname)
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
            self.logger.debug("danmaku parse failed: event=%s", event)
            return

        uid, text, moderator_hint = parsed
        self.logger.debug("danmaku received: uid=%s text=%s (anchor_uid=%s)", uid, text, self.config.anchor_uid)

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
                    await self._enqueue_message(text=rule.reply_template, reply_uid=uid)
                    return

        command = _parse_command(text)
        if command is None:
            self.logger.debug("danmaku is not command: uid=%s text=%s", uid, text)
            # Check keyword-based auto-reply
            kr = self.config.features.keyword_reply
            self.logger.debug("keyword_reply config: enabled=%s rules_count=%s", kr.enabled, len(kr.rules))
            if kr.enabled and kr.rules:
                reply = _match_keyword_rule(text, kr.rules)
                if reply:
                    now = time.time()
                    last = self._keyword_reply_cooldown_ts.get(uid, 0.0)
                    if now - last >= kr.cooldown_seconds:
                        self._keyword_reply_cooldown_ts[uid] = now
                        self.logger.debug("keyword reply matched: uid=%s text=%s reply=%s", uid, text, reply)
                        await self._enqueue_message(text=reply, reply_uid=uid)
            return

        name, arg = command
        self.logger.debug("command parsed: uid=%s name=%s arg=%s", uid, name, arg)
        if name == "blindbox_me":
            self.logger.info("blindbox stats requested: uid=%s arg=%s", uid, arg)
            now = datetime.now()
            month_key = now.strftime("%Y-%m")
            month_label = f"{now.month}月"
            if arg:
                result = self.store.get_user_monthly_blindbox_by_uname(month=month_key, uname=arg)
                if result is None:
                    await self._enqueue_message(text=f"未找到{arg}的盲盒记录", reply_uid=uid)
                    return
                _uid, blind_count, cost_total, actual_total, profit_total = result
                text_out = f"{month_label}{arg}盲盒{blind_count}个，总支出{cost_total}，总收益{profit_total}"
            else:
                # If anchor or admin, show total stats of the room
                if uid == self.config.anchor_uid or self._has_control_permission(uid, moderator_hint):
                    total = self.store.get_monthly_total_blindbox(month=month_key)
                    blind_count, cost_total, actual_total, profit_total = total
                    text_out = f"{month_label}本直播间盲盒{blind_count}个，总支出{cost_total}，总收益{profit_total}"
                else:
                    row = self.store.get_user_monthly_blindbox(month=month_key, uid=uid)
                    if row is None:
                        gift_event_count, _ = self.store.get_user_monthly_gift_activity(month=month_key, uid=uid)
                        text_out = f"{month_label} 暂无盲盒记录" if gift_event_count > 0 else f"{month_label} 无送礼记录"
                        await self._enqueue_message(text=text_out, reply_uid=uid)
                        return
                    blind_count, cost_total, actual_total, profit_total = row
                    text_out = f"{month_label}盲盒{blind_count}个，支出{cost_total}，收益{profit_total}"
            await self._enqueue_message(text=text_out, reply_uid=uid)
            return

        if name == "checkin":
            parsed = _parse_danmaku_user_and_text(event)
            uname = parsed[1] if parsed else "用户"
            days, rank = self.store.user_checkin(uid, uname)
            msg = f"感谢{uname}签到！连续签到{days}天，目前排名第{rank}。继续坚持喵~"
            await self._enqueue_message(text=msg, reply_uid=uid)
            return

        if name == "fortune":
            fortunes = [
                ("大吉", ["今天运气爆棚，做什么都顺风顺水！", "主播都被你的欧气惊到了！"]),
                ("中吉", ["运势不错，是个适合发财的好日子。", "心情舒畅，会有好事发生哦。"]),
                ("小吉", ["平稳的一天，适合静下心来干大事。", "顺其自然，好运自会到来。"]),
                ("末吉", ["虽然平淡，但健康平安就是最大的福气。", "不要急躁，慢慢来总会好的。"]),
                ("凶", ["今天适合低调行事，多看看直播转转运。", "别灰心，下次抽签一定是上签！"]),
                ("大凶", ["生活总有低谷，吃顿好的安慰一下自己吧。", "多发几条弹幕，霉运都会跑掉的。"]),
            ]
            f_type, jokes = random.choice(fortunes)
            joke = random.choice(jokes)
            msg = f"抽签结果：【{f_type}】！{joke}"
            await self._enqueue_message(text=msg, reply_uid=uid)
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
            self._welcome_enabled = True
            await self._enqueue_message(text="已开启欢迎", reply_uid=uid)
            return

        if name == "welcome_off":
            self._welcome_enabled = False
            await self._enqueue_message(text="已关闭欢迎", reply_uid=uid)
            return

        if name == "welcome_text":
            if not arg:
                await self._enqueue_message(text="用法：#欢迎 词 <欢迎词模板>", reply_uid=uid)
                return
            self._welcome_template = arg
            await self._enqueue_message(text="欢迎词已更新", reply_uid=uid)
            return

    async def _on_all_events(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "?")
        
        # Handle PK Battle Start
        if event_type == "PK_BATTLE_SETTLE_USER": # Note: Some events are not prefixed with PK_BATTLE_ in old versions but nemo2011 uses generic labels
            pass 

        if event_type == "PK_BATTLE_START":
            self.logger.info("PK battle started, processing stats...")
            await self._handle_pk_start(event)
            return

        # Suppress high-frequency / known events to reduce log noise
        noisy_prefixes = ("SUPER_CHAT", "HOT_RANK_", "ONLINE_RANK_",
                          "LIKE_INFO_V3_", "POPULARITY_")
        if event_type.startswith(noisy_prefixes):
            return
        noisy_exact = ("VIEW", "INTERACT_WORD_V2", "WATCHED_CHANGE",
                       "ROOM_REAL_TIME_MESSAGE_UPDATE", "NOTICE_MSG",
                       "LIVE", "PREPARING", "ENTRY_EFFECT", "ROOM_CHANGE",
                       "COMBO_RESOURCE", "COMBO_SEND", "ANIMATION",
                       "SPECIAL_GIFT", "VERIFICATION_SUCCESSFUL",
                       "UNIVERSAL_EVENT_GIFT", "UNIVERSAL_EVENT_GIFT_V2",
                       "GUARD_BUY", "USER_TOAST_MSG", "USER_TOAST_MSG_V2")
        if event_type in noisy_exact:
            return
        self.logger.debug("unhandled event: type=%s", event_type)

    async def _handle_pk_start(self, event: dict[str, Any]) -> None:
        try:
            data = event.get("data", {})
            # room_id is self, init_info.room_id is opponent
            init_info = data.get("init_info", {})
            
            opp_name = init_info.get("anchor_name", "对面主播")
            opp_guard = init_info.get("guard_count", 0)
            opp_online = init_info.get("online_count", 0)
            
            msg = f"⚔️ PK开始！对面：{opp_name}\n"
            msg += f"📊 对方舰长：{opp_guard} | 在线：{opp_online}"
            
            await self._enqueue_message(text=msg, reply_uid=None)
        except Exception as exc:
            self.logger.error("Error processing PK_BATTLE_START: %s", exc)

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
        self.logger.debug("message enqueued: reply_uid=%s text=%s", reply_uid, text)
        await self._msg_queue.put(OutboundMessage(text=text, reply_uid=reply_uid))


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return 0


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


def _parse_danmaku_user_and_text(event: dict[str, Any]) -> Optional[tuple[int, str, bool]]:
    data = event.get("data", {})
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, list) or len(info) < 3:
        return None

    text = str(info[1]) if len(info) > 1 else ""
    user_info = info[2]
    if not isinstance(user_info, list) or not user_info:
        return None

    uid = _safe_int(user_info[0])

    moderator_hint = False
    if len(user_info) >= 3:
        moderator_hint = _safe_int(user_info[2]) in (1, 2, 3)

    return uid, text.strip(), moderator_hint


def _parse_command(text: str) -> Optional[tuple[str, str]]:
    s = text.strip()
    # Normalize full-width '#' and spaces
    if s.startswith("＃"):
        s = "#" + s[1:]
    
    # Create a normalized compact version for simple commands
    compact = "".join(s.split()).replace("：", ":")

    # Command: #签到
    if compact == "#签到":
        return "checkin", ""

    # Command: #抽签
    if compact == "#抽签":
        return "fortune", ""

    # Command: #盲盒统计 / #盲盒我的
    if compact in ("#盲盒统计", "#盲盒我的"):
        return "blindbox_me", ""
    
    # Command: #盲盒统计:名字 or #盲盒统计 名字
    if compact.startswith("#盲盒统计:"):
        return "blindbox_me", compact[len("#盲盒统计:"):]
    
    # Fallback for "#盲盒统计 名字" where compact would be "#盲盒统计名字"
    if s.startswith("#盲盒统计 ") or s.startswith("＃盲盒统计 "):
        return "blindbox_me", s[len("#盲盒统计 "):].strip()

    if compact in ("#欢迎:开", "＃欢迎:开"):
        return "welcome_on", ""
    if compact in ("#欢迎:关", "＃欢迎:关"):
        return "welcome_off", ""

    # Flexible matching for commands with spaces
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


def _match_keyword_rule(text: str, rules: list[KeywordRule]) -> Optional[str]:
    for rule in rules:
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
