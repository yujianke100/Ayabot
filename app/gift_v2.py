"""B 站直播「SEND_GIFT_V2」事件解码器。

2026-07 起 B 站在灰度新协议：送礼事件名为 `SEND_GIFT_V2`，礼物数据不再是
JSON 明文，而是放在 `data.data.pb`（base64 编码的 protobuf）。旧代码只监听
`SEND_GIFT`，所以新协议下收不到任何礼物事件 —— 统计全部为 0。

这里用纯标准库实现 protobuf wire format 解析（不依赖 protobuf / pure_protobuf），
把新协议还原成与旧 `SEND_GIFT` JSON 等价的 payload，交给既有统计逻辑消费。

字段编号来自 B 站 web 端 `SendGiftBroadcast` 定义：

    message SendGiftBroadcast {
      uint64 uid = 1;
      string uname = 2;
      string face = 3;
      uint32 guard_level = 5;
      MedalInfo medal_info = 8;      // 1 target_id, 4 anchor_roomid, 5 medal_level, 6 medal_name
      BlindGift blind_gift = 9;      // 3 original_gift_name, 6 original_gift_price
      repeated GiftItem gift_list = 10;
    }
    message GiftItem {
      uint64 gift_id = 1;
      string gift_name = 2;
      uint64 num = 3;
      uint64 gift_type = 4;
      uint64 price = 5;       // 金瓜子单价；送盲盒时是「爆出礼物」的单价
      uint64 total_coin = 7;  // 实付总额；盲盒 = 盲盒单价 * num
      string coin_type = 8;
      string tid = 9;
      uint64 timestamp = 10;
      string rnd = 12;
      string action = 18;
      GiftMaterialSnapShot gift_info = 35;  // 1 img_basic
    }

一个事件里 `gift_list` 可能有多件礼物（盲盒批量），必须逐件处理。
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Optional

# ── protobuf wire format 基础解析 ────────────────────────────

_WIRE_VARINT = 0
_WIRE_64BIT = 1
_WIRE_LEN = 2
_WIRE_32BIT = 5


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("unexpected end of varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _iter_fields(buf: bytes):
    """逐个产出 (field_number, wire_type, value)。

    varint → int；length-delimited → bytes；32/64 bit → bytes。
    """
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = _read_varint(buf, pos)
        field_no = key >> 3
        wire = key & 0x07
        if field_no == 0:
            raise ValueError("invalid field number 0")
        if wire == _WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
            yield field_no, wire, value
        elif wire == _WIRE_LEN:
            length, pos = _read_varint(buf, pos)
            if pos + length > end:
                raise ValueError("length-delimited field overruns buffer")
            yield field_no, wire, buf[pos:pos + length]
            pos += length
        elif wire == _WIRE_64BIT:
            yield field_no, wire, buf[pos:pos + 8]
            pos += 8
        elif wire == _WIRE_32BIT:
            yield field_no, wire, buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")


def _as_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    return 0


def _as_str(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return ""


def _parse_message(buf: bytes) -> dict[int, list[Any]]:
    """把一条 message 解析成 {字段号: [值, ...]}。解析失败时返回已解析部分。"""
    out: dict[int, list[Any]] = {}
    try:
        for field_no, _wire, value in _iter_fields(buf):
            out.setdefault(field_no, []).append(value)
    except ValueError:
        pass
    return out


def _first_int(fields: dict[int, list[Any]], key: int, default: int = 0) -> int:
    values = fields.get(key)
    if not values:
        return default
    return _as_int(values[0])


def _first_str(fields: dict[int, list[Any]], key: int, default: str = "") -> str:
    values = fields.get(key)
    if not values:
        return default
    return _as_str(values[0])


def _first_msg(fields: dict[int, list[Any]], key: int) -> dict[int, list[Any]]:
    values = fields.get(key)
    if not values or not isinstance(values[0], (bytes, bytearray)):
        return {}
    return _parse_message(bytes(values[0]))


# ── SEND_GIFT_V2 → 旧 SEND_GIFT 等价 payload ────────────────


def decode_send_gift_v2(pb_b64: str) -> list[dict[str, Any]]:
    """解码 SEND_GIFT_V2 的 base64 protobuf，返回礼物列表（每件礼物一项）。

    每项都构造成与旧 `SEND_GIFT` 的 `data.data` JSON 相同的键名，方便复用
    既有的 `_extract_blindbox_profit` / 数据库写入逻辑。
    """
    if not pb_b64:
        return []
    try:
        raw = base64.b64decode(pb_b64, validate=False)
    except (binascii.Error, ValueError):
        return []
    if not raw:
        return []

    root = _parse_message(raw)
    uid = _first_int(root, 1)
    uname = _first_str(root, 2)
    face = _first_str(root, 3)
    guard_level = _first_int(root, 5)

    # 少数事件 uid 不在 1 号字段，从 sender_uinfo(15) 里兜底取
    if uid <= 0:
        sender = _first_msg(root, 15)
        uid = _first_int(sender, 1)
        if not uname:
            base = _first_msg(sender, 2)
            uname = _first_str(base, 1)
            if not face:
                face = _first_str(base, 2)

    medal_root = _first_msg(root, 8)
    medal_info: Optional[dict[str, Any]] = None
    if medal_root:
        medal_info = {
            "target_id": _first_int(medal_root, 1),
            "anchor_roomid": _first_int(medal_root, 4),
            "medal_level": _first_int(medal_root, 5),
            "medal_name": _first_str(medal_root, 6),
        }

    blind_root = _first_msg(root, 9)
    blind_name = _first_str(blind_root, 3)
    blind_price = _first_int(blind_root, 6)
    blind_gift: Optional[dict[str, Any]] = None
    if blind_name or blind_price:
        blind_gift = {
            "original_gift_name": blind_name,
            "original_gift_price": blind_price,
        }

    gift_bufs = [v for v in root.get(10, []) if isinstance(v, (bytes, bytearray))]
    if not gift_bufs:
        return []

    payloads: list[dict[str, Any]] = []
    for gift_buf in gift_bufs:
        item = _parse_message(bytes(gift_buf))
        gift_num = max(_first_int(item, 3), 1)
        gift_price = _first_int(item, 5)
        item_blind = dict(blind_gift) if blind_gift else None
        if item_blind is not None:
            # 旧逻辑用 blind_gift.gift_tip_price 表示「爆出礼物单价」，
            # 新协议里这个值就是 gift item 自身的 price。
            item_blind["gift_tip_price"] = gift_price

        payload: dict[str, Any] = {
            "uid": uid,
            "uname": uname,
            "face": face,
            "guard_level": guard_level,
            "giftId": _first_int(item, 1),
            "giftName": _first_str(item, 2),
            "num": gift_num,
            "giftType": _first_int(item, 4),
            "price": gift_price,
            "total_coin": _first_int(item, 7),
            "coin_type": _first_str(item, 8),
            "tid": _first_str(item, 9),
            "timestamp": _first_int(item, 10),
            "rnd": _first_str(item, 12),
            "action": _first_str(item, 18),
            "blind_gift": item_blind,
            "medal_info": medal_info,
            "src_protocol": "SEND_GIFT_V2",
        }
        gift_info = _first_msg(item, 35)
        if gift_info:
            payload["gift_info"] = {"img_basic": _first_str(gift_info, 1)}
        payloads.append(payload)
    return payloads
