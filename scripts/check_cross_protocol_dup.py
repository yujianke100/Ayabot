"""跨协议重复计数 + 上舰缺昵称被整条丢掉。

B站 正在灰度新礼物协议（SEND_GIFT_V2 / pb）。灰度期**同一件礼物会同时以
旧协议 JSON 和新协议 pb 推两次**，谁先到不确定。去重逻辑以前写成
`if is_v2 and seen is not None` —— 只对「新协议那一条」判断：

    V2 先到 → 旧协议后到 → 旧那条被当成新礼物**又记一遍**
    （条数、金额、排行全部翻倍，而日志里两条都写着「正常记录」）

这里逐条验证双向去重，并且守住「不能误杀真礼物」这一侧：
同一秒连送两件同名同数量的礼物，如果没有 tid/rnd，必须都记上。

顺带验证上舰（大航海）：`_extract_guard_buy_payload` 以前是
`if uid <= 0 or not uname: return None` —— 少一个昵称字段就整条丢弃，
既没有记录（大航海页面永远是空的）也没有致谢。和礼物那边同一个教训。

    ./.venv/Scripts/python.exe scripts/check_cross_protocol_dup.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UID = 88123


class _FakeStore:
    def __init__(self) -> None:
        self.rows: list = []

    def record_gift_event(self, event, dedupe_key: str = "", source: str = "",
                          commit: bool = True) -> bool:
        self.rows.append(event)
        return True

    def __getattr__(self, name):  # noqa: ANN001
        def _noop(*a, **k):
            return None
        return _noop


def main() -> int:
    from app.bot import LiveRobot, _extract_guard_buy_payload
    from app.config import load_config

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="crossdup_"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text((ROOT / "config.example.yaml").read_text(encoding="utf-8"),
                            encoding="utf-8")
        cfg = load_config(str(cfg_file))

        class _FakeAuth:
            status = "ok"

            def write_status(self, *a, **k) -> None:
                pass

            def mark_needs_login(self, *a, **k) -> None:
                pass

        def new_robot() -> tuple[LiveRobot, _FakeStore]:
            r = LiveRobot(config=cfg, credential=object(), auth=_FakeAuth())
            store = _FakeStore()
            r.store = store  # type: ignore[assignment]
            r._enqueue_reply = lambda *a, **k: None  # type: ignore[assignment]
            return r, store

        def send(robot: LiveRobot, *, v2: bool, tid: str = "", num: int = 1,
                 ts: int = 1780000000) -> None:
            payload = {
                "uid": UID, "uname": "甲", "giftName": "辣条", "num": num,
                "timestamp": ts, "total_coin": 100 * num,
            }
            if tid:
                payload["tid"] = tid
            if v2:
                payload["src_protocol"] = "SEND_GIFT_V2"
            asyncio.run(robot._handle_gift_payload(payload, "SEND_GIFT"))

        print("[1] 新协议先到、旧协议后到 → 只记一条")
        robot, store = new_robot()
        send(robot, v2=True, tid="tid-cross-1")
        send(robot, v2=False, tid="tid-cross-1")
        ok("只写了一行", len(store.rows) == 1, len(store.rows))
        ok("重复计数 +1", robot._dropped_gift_dup == 1, robot._dropped_gift_dup)

        print("[2] 旧协议先到、新协议后到 → 也只记一条（原来的方向）")
        robot, store = new_robot()
        send(robot, v2=False, tid="tid-cross-2")
        send(robot, v2=True, tid="tid-cross-2")
        ok("只写了一行", len(store.rows) == 1, len(store.rows))
        ok("重复计数 +1", robot._dropped_gift_dup == 1, robot._dropped_gift_dup)

        print("[3] 同一秒连送两件同名同数量、**没有 tid** → 两件都要记上")
        robot, store = new_robot()
        send(robot, v2=False, ts=1780000001)
        send(robot, v2=False, ts=1780000001)
        ok("记了两行（不能因为兜底键相同就误杀真礼物）", len(store.rows) == 2, len(store.rows))
        ok("没有被算成重复", robot._dropped_gift_dup == 0, robot._dropped_gift_dup)

        print("[4] V2 一个事件打包 3 件同款、缺 tid → 三件都要记（靠 _v2_index）")
        robot, store = new_robot()
        for idx in range(3):
            payload = {
                "uid": UID, "uname": "甲", "giftName": "心动盲盒", "num": 1,
                "timestamp": 1780000002, "total_coin": 15000, "src_protocol": "SEND_GIFT_V2",
                "_v2_index": idx, "giftId": 33988,
            }
            asyncio.run(robot._handle_gift_payload(payload, "SEND_GIFT"))
        ok("记了三行", len(store.rows) == 3, len(store.rows))

        print("[5] 上舰缺昵称不再整条丢弃")
        ev = {"type": "GUARD_BUY", "data": {"cmd": "GUARD_BUY", "data": {
            "uid": 45678, "guard_level": 3, "num": 1, "price": 198000,
            "start_time": 1780000000,
        }}}
        got = _extract_guard_buy_payload(ev)
        ok("解析出来了（以前返回 None）", got is not None, got)
        if got:
            ok("昵称用占位", got["uname"] == "用户45678", got["uname"])
            ok("金额按金瓜子 ÷100 = 1980 电池", got["value"] == 1980, got["value"])
            ok("舰长识别正确", got["guard_name"] == "舰长", got["guard_name"])

        print("[6] 上舰没有 uid 仍然要丢（无法归属）")
        ev2 = {"type": "GUARD_BUY", "data": {"cmd": "GUARD_BUY", "data": {
            "username": "无id观众", "guard_level": 3, "num": 1, "price": 198000,
        }}}
        ok("返回 None", _extract_guard_buy_payload(ev2) is None)

        print("[7] 结构性断言（防止修复被回退）")
        bot_src = (ROOT / "app" / "bot.py").read_text(encoding="utf-8").replace("\r\n", "\n")
        ok("去重条件同时覆盖两个方向",
           "(is_v2 or bool(tid_or_rnd))" in bot_src)
        ok("没有昵称走占位而不是 return", 'uname = f"用户{uid}"' in bot_src)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("CROSS PROTOCOL DUP OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
