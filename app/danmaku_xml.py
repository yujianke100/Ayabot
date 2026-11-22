"""录播弹幕 XML 解析：把历史录播的弹幕文件还原成可入库的礼物 / 上舰事件。

为什么需要它
------------
机器人只能记录「运行时」的礼物，用户想补历史排行只能从录播工具导出的弹幕 XML 里捞。
本模块只负责**解析与换算**，不碰数据库；写库在 `app/storage.py`（带 dedupe_key，可重复导入）。

支持的 XML 形态（BililiveRecorder / 录播姬，兼容 mikufans 主站格式）
------------------------------------------------------------------
    <i>
      <metadata><record_start_time>2024-12-01T18:05:02+08:00</record_start_time>…</metadata>
      <BililiveRecorderRecordInfo roomid name start_time/>          <!-- 新版，ISO8601 -->
      <d p="12.345,1,25,16777215,1750000000000,0,<uid>,0" uid="" user="昵称">弹幕文本</d>
      <gift ts="13.000" uid="123" user="昵称" giftname="辣条" giftcount="1" [raw='{…}']/>
      <guard ts="18.000" uid="123" user="昵称" level="3" count="1" [raw='{…}']/>
      <sc ts="50.000" uid="123" user="昵称" price="30" time="60">醒目留言</sc>
    </i>

时间口径
--------
`<gift>` / `<guard>` / `<sc>` 的 `ts` 是**相对录制开始**的秒数（浮点），不是时间戳。
换算成绝对时间必须拿到录制起始时间，三种来源按优先级：

1. `<metadata><record_start_time>`（ISO8601，带时区）
2. `<BililiveRecorderRecordInfo start_time>`
3. 从 `<d>` 的 `p` 第 5 段（发送时间戳，毫秒）反推 base = send_ts/1000 - p[0]

三者都没有时**不可导入**（无法确定是哪一天的数据），直接报错，绝不瞎猜。

金额口径（与实时记录保持一致）
------------------------------
实时事件里 `raw_json.total_coin` 是金瓜子（1000 金瓜子 = 1 元），排行 SQL 用 /1000 换算成元。
录播姬**默认不写 raw**（`RecordDanmakuRaw` 默认关闭），此时 XML 里只有礼物名和数量、没有金额，
只能按礼物名查表估算：

  * 有 `raw`                     → 用真实字段，盲盒也能完整还原（最准）
  * 无 `raw`、礼物名查得到价格   → price × giftcount
  * 无 `raw`、礼物名查不到价格   → 数量照样入库，金额记 0，并在结果里报「未知价格」条数

盲盒：XML 里的开盒结果就是普通的 `<gift>`，**没有 raw 时无法区分是否盲盒**，
所以无 raw 的 XML 不做盲盒统计（不会把普通礼物误判成盲盒，也不会算出假的盈亏）。
"""
from __future__ import annotations

import io
import json
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime

# 大航海单价（金瓜子），按 guard_level：B站 只有这三档
GUARD_UNIT_PRICE = {3: 198000, 2: 1998000, 1: 19998000}
GUARD_NAMES = {1: "总督", 2: "提督", 3: "舰长"}

# 一条录播 XML 允许的最大体积：再大基本是导错了文件
MAX_XML_BYTES = 256 * 1024 * 1024

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


@dataclass(slots=True)
class GiftRec:
    ts: int
    uid: int
    uname: str
    name: str
    num: int
    raw: dict | None = None


@dataclass(slots=True)
class GuardRec:
    ts: int
    uid: int
    uname: str
    level: int
    months: int
    raw: dict | None = None


@dataclass(slots=True)
class ParseResult:
    gifts: list[GiftRec] = field(default_factory=list)
    guards: list[GuardRec] = field(default_factory=list)
    chat_count: int = 0
    sc_count: int = 0
    base_ts: int | None = None
    base_source: str = ""
    start_ts: int | None = None
    end_ts: int | None = None
    dropped_no_uid: int = 0
    dropped_bad: int = 0
    has_raw: bool = False
    warnings: list[str] = field(default_factory=list)
    room_id_hint: str = ""
    anchor_hint: str = ""
    # 相对录制开始的秒数（解析时先收着，base 确定后再换算成绝对时间）
    gift_rel: list[float] = field(default_factory=list)
    guard_rel: list[float] = field(default_factory=list)


class DanmakuXmlError(ValueError):
    """XML 无法用于导入（格式不对 / 缺少录制时间）。"""


def _safe_int(val, default: int = 0) -> int:
    try:
        if val is None or val == "":
            return default
        return int(float(str(val).strip()))
    except (TypeError, ValueError):
        return default


def _parse_iso_ts(text: str) -> int | None:
    """把 ISO8601 时间串转成时间戳（支持 +08:00 与 Z）。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        pass
    m = _ISO_RE.match(text)
    if m:
        try:
            return int(datetime(*(int(g) for g in m.groups())).timestamp())
        except ValueError:
            return None
    return None


def _parse_p(p: str) -> tuple[float, int]:
    """`p="12.345,1,25,color,send_ts_ms,pool,uid,rowid"` → (出现秒数, 发送时间戳ms)。"""
    parts = (p or "").split(",")
    try:
        appear = float(parts[0]) if parts and parts[0].strip() else 0.0
    except ValueError:
        appear = 0.0
    send_ms = 0
    if len(parts) >= 5:
        send_ms = _safe_int(parts[4])
        # 主站弹幕 XML 的 p[4] 也是毫秒时间戳，但可能为 0
        if send_ms < 946684800000:  # < 2000-01-01
            send_ms = 0
    return appear, send_ms


def _load_json_attr(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict):
        # 录播姬写的是 RawObject["data"]
        data = obj.get("data")
        return data if isinstance(data, dict) else obj
    return None


def parse_danmaku_xml(data: bytes, filename: str = "") -> ParseResult:
    """解析一份录播弹幕 XML。失败抛 DanmakuXmlError（带人能看懂的原因）。"""
    if not data:
        raise DanmakuXmlError("文件是空的")
    if len(data) > MAX_XML_BYTES:
        raise DanmakuXmlError(
            f"文件太大（{len(data) / 1048576:.1f} MB），上限 {MAX_XML_BYTES // 1048576} MB")

    res = ParseResult()
    meta_record_start = ""
    info_start = ""
    derived_base: int | None = None
    first_appear: float | None = None

    # 逐元素解析，避免把几十 MB 的树整个建起来
    try:
        for _event, el in ET.iterparse(io.BytesIO(data), events=("end",)):
            tag = el.tag.lower() if isinstance(el.tag, str) else ""
            if tag == "record_start_time":
                meta_record_start = (el.text or "").strip()
            elif tag.endswith("recordinfo"):
                info_start = (el.get("start_time") or "").strip()
                res.room_id_hint = (el.get("roomid") or "").strip()
                res.anchor_hint = (el.get("name") or "").strip()
            elif tag == "d":
                res.chat_count += 1
                appear, send_ms = _parse_p(el.get("p") or "")
                if derived_base is None and send_ms:
                    derived_base = int(send_ms / 1000 - appear)
                if first_appear is None:
                    first_appear = appear
            elif tag == "gift":
                uid = _safe_int(el.get("uid"))
                name = (el.get("giftname") or "").strip()
                num = _safe_int(el.get("giftcount"), 1)
                rel = _safe_float(el.get("ts"))
                if uid <= 0 or not name:
                    res.dropped_no_uid += 1
                    continue
                raw = _load_json_attr(el.get("raw"))
                if raw:
                    res.has_raw = True
                res.gifts.append(GiftRec(
                    ts=0,  # 相对时间先记在 rel_ts 里，等 base 确定后再补
                    uid=uid,
                    uname=(el.get("user") or "").strip(),
                    name=name,
                    num=max(num, 1),
                    raw=raw,
                ))
                res.gift_rel.append(rel)
            elif tag == "guard":
                uid = _safe_int(el.get("uid"))
                level = _safe_int(el.get("level"))
                months = max(_safe_int(el.get("count"), 1), 1)
                rel = _safe_float(el.get("ts"))
                if uid <= 0 or level not in GUARD_UNIT_PRICE:
                    res.dropped_no_uid += 1
                    continue
                raw = _load_json_attr(el.get("raw"))
                if raw:
                    res.has_raw = True
                res.guards.append(GuardRec(
                    ts=0,
                    uid=uid,
                    uname=(el.get("user") or "").strip(),
                    level=level,
                    months=months,
                    raw=raw,
                ))
                res.guard_rel.append(rel)
            elif tag == "sc":
                res.sc_count += 1
            el.clear()
    except ET.ParseError as exc:
        raise DanmakuXmlError(f"不是合法的 XML：{exc}") from exc

    # 补绝对时间
    base = None
    if meta_record_start:
        base = _parse_iso_ts(meta_record_start)
        if base:
            res.base_source = "metadata.record_start_time"
    if base is None and info_start:
        base = _parse_iso_ts(info_start)
        if base:
            res.base_source = "BililiveRecorderRecordInfo.start_time"
    if base is None and derived_base:
        base = derived_base
        res.base_source = "弹幕时间戳反推"

    gift_rels = res.gift_rel
    guard_rels = res.guard_rel
    if not res.gifts and not res.guards:
        raise DanmakuXmlError(
            "文件里没有可用的事件（礼物 0 条、上舰 0 条）。"
            "如果录制时没开「弹幕录制-礼物」，这个 XML 里就只有普通弹幕，无法补历史排行。")

    if base is None:
        raise DanmakuXmlError(
            "这份 XML 没有录制起始时间（<metadata> 和 <d p> 里都没有可靠时间戳），"
            "无法确定是哪一天的数据，不能导入。")

    res.base_ts = base
    for rec, rel in zip(res.gifts, gift_rels):
        rec.ts = int(base + rel)
    for rec, rel in zip(res.guards, guard_rels):
        rec.ts = int(base + rel)
    stamps = [r.ts for r in res.gifts] + [r.ts for r in res.guards]
    if stamps:
        res.start_ts, res.end_ts = min(stamps), max(stamps)

    if not res.has_raw:
        res.warnings.append(
            "这份 XML 没有保存原始数据（录播姬的「保存原始数据」默认关闭），"
            "礼物金额是按礼物名估算的；盲盒无法还原。")
    return res


def _safe_float(val) -> float:
    try:
        return float(str(val).strip()) if val not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def dedupe_key_for(*parts) -> str:
    """稳定去重键：同一份 XML 重复导入、或分片重叠时不会重复计数。"""
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════
#  解析结果 → 入库事件
# ══════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class ImportPlan:
    """一次导入要写的全部内容（预检时只看它，不落库）。"""
    gifts: list[tuple[object, str]] = field(default_factory=list)   # (GiftEvent, dedupe_key)
    guards: list[dict] = field(default_factory=list)                # record_guard_event 的关键字参数
    stats: dict = field(default_factory=dict)


def _blindbox_profit(payload: dict) -> dict:
    """复用实时链路上的盲盒判定，避免两套口径。"""
    from app.bot import _extract_blindbox_profit  # 延迟导入：app.bot 会拉起 blivedm 等依赖
    return _extract_blindbox_profit(payload)


def build_plan(
    parsed: ParseResult,
    price_map: dict[str, tuple[int, str]],
    source: str,
    *,
    room_name: str = "",
) -> ImportPlan:
    """把解析结果换算成入库事件。

    price_map: 礼物名 → (单价, 币种)，来自 B站礼物配置；查不到时单价记 0。
    source:    写入 gift_events.source 的标记（形如 xml:文件名），用于事后撤销。
    """
    from app.storage import GiftEvent, StatsStore

    plan = ImportPlan()
    seq: dict[tuple, int] = {}
    unknown_names: dict[str, int] = {}
    blind_count = 0
    value_cent = 0          # 预检用：累计价值（元 × 100，避免浮点误差）
    total_num = 0

    for rec in parsed.gifts:
        payload: dict
        if rec.raw:
            payload = dict(rec.raw)
            payload.setdefault("giftName", rec.name)
            payload.setdefault("num", rec.num)
        else:
            unit, coin = price_map.get(rec.name, (0, "gold"))
            if unit <= 0:
                unknown_names[rec.name] = unknown_names.get(rec.name, 0) + 1
            payload = {
                "giftName": rec.name,
                "num": rec.num,
                "price": unit,
                "discount_price": unit,
                "total_coin": unit * rec.num,
                "coin_type": coin,
                "blind_gift": None,
            }
            if unit <= 0:
                payload["price_unknown"] = True

        payload["uid"] = rec.uid
        payload["uname"] = rec.uname
        payload["timestamp"] = rec.ts
        payload["src_protocol"] = payload.get("src_protocol") or "XML_IMPORT"
        payload["xml_import"] = True
        payload["xml_source"] = source

        blind = _blindbox_profit(payload)
        if blind["is_blind_box"]:
            blind_count += 1
            value_cent += int(blind["actual_value"]) * 10      # 实际价值：电池 ÷10 = 元
        else:
            coin = _safe_int(payload.get("total_coin"))
            value_cent += coin // 10                            # 金瓜子 ÷1000 = 元 >> ×100
        total_num += rec.num

        key = dedupe_key_for("xml", rec.uid, rec.ts, rec.name, rec.num,
                             seq.get((rec.uid, rec.ts, rec.name, rec.num), 0))
        seq[(rec.uid, rec.ts, rec.name, rec.num)] = seq.get((rec.uid, rec.ts, rec.name, rec.num), 0) + 1

        plan.gifts.append((
            GiftEvent(
                ts=rec.ts,
                month=StatsStore.month_of_ts(rec.ts),
                uid=rec.uid,
                uname=rec.uname,
                event_type="SEND_GIFT",
                gift_name=rec.name,
                gift_num=rec.num,
                is_blind_box=1 if blind["is_blind_box"] else 0,
                blind_box_cost=blind["blind_box_cost"],
                actual_value=blind["actual_value"],
                profit_value=blind["profit_value"],
                raw_json=json.dumps(payload, ensure_ascii=False),
            ),
            key,
        ))

    guard_value_coin = 0
    for rec in parsed.guards:
        unit = GUARD_UNIT_PRICE.get(rec.level, 0)
        months = rec.months
        start_time = rec.ts * 1000
        if rec.raw:
            raw_price = _safe_int(rec.raw.get("price"))
            raw_num = _safe_int(rec.raw.get("num"))
            if raw_price > 0:
                unit = raw_price
            if raw_num > 0:
                months = raw_num
            start_time = _safe_int(rec.raw.get("start_time")) or start_time
        price_coin = unit * months
        guard_value_coin += price_coin
        plan.guards.append(dict(
            ts=rec.ts,
            month=StatsStore.month_of_ts(rec.ts),
            uid=rec.uid,
            uname=rec.uname,
            guard_level=rec.level,
            guard_name=GUARD_NAMES.get(rec.level, ""),
            months=months,
            price_coin=price_coin,
            value=price_coin // 100,
            start_time=start_time,
            source=source,
            raw_json=json.dumps({"xml_import": True, "xml_source": source,
                                 "level": rec.level, "count": months}, ensure_ascii=False),
        ))

    plan.stats = {
        "gifts": len(plan.gifts),
        "gift_num": total_num,
        "guards": len(plan.guards),
        "blindbox": blind_count,
        "value_yuan": round(value_cent / 100, 2),
        "guard_value_yuan": round(guard_value_coin / 1000, 2),
        "unknown_price_kinds": len(unknown_names),
        "unknown_price_events": sum(unknown_names.values()),
        "unknown_price_names": sorted(unknown_names)[:12],
        "chat": parsed.chat_count,
        "sc": parsed.sc_count,
        "dropped_no_uid": parsed.dropped_no_uid,
        "has_raw": parsed.has_raw,
        "base_source": parsed.base_source,
        "start_ts": parsed.start_ts,
        "end_ts": parsed.end_ts,
        "warnings": list(parsed.warnings),
    }
    return plan
