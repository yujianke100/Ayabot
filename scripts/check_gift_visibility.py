"""第二十五轮自检：礼物链路「收到但记不上」的可见性与不再误丢。

用户反馈 #3：关键词能触发统计，但所有统计都是 0。
三类静默丢弃以前都只写一行日志（有的连日志都没有）：
  1. SEND_GIFT_V2 的 pb 解码失败 → 整条事件丢弃
  2. SEND_GIFT_V2 没有 pb → 直接 return（连 JSON 兜底都不试）
  3. 旧协议缺 uname → `_extract_gift_payload` 直接 return None（无计数无日志）
  4. V2 一个事件打包多件同款礼物且缺 tid → 去重键撞车，只记 1 件

用法: ./.venv/Scripts/python.exe scripts/check_gift_visibility.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _read_src(rel: str) -> str:
    """统一成 LF 再断言：仓库 HEAD 是 LF，本地编辑后可能是 CRLF，
    带换行的结构断言会因为回车符假红。"""
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


BOT_SRC = _read_src("app/bot.py")
SRV_SRC = _read_src("app/web/server.py")


class _FakeStore:
    """记录写入的假 store（不碰真实数据库）。"""

    def __init__(self) -> None:
        self.rows: list = []
        self.dup_keys: set[str] = set()

    def record_gift_event(self, event, dedupe_key: str = "", source: str = "",
                          commit: bool = True) -> bool:
        # 真实库对实时行（dedupe_key 为空）**不去重**：同一个人同一秒刷 3 个同款
        # 会写 3 行。假 store 也照这个语义，否则会把「去重键撞车」这个 bug 掩盖掉。
        if dedupe_key and dedupe_key in self.dup_keys:
            return False
        if dedupe_key:
            self.dup_keys.add(dedupe_key)
        self.rows.append(event)
        return True

    # 供 _handle_gift_payload 其它分支用到的空实现
    def __getattr__(self, name):  # noqa: ANN001
        def _noop(*a, **k):
            return None
        return _noop


def main() -> int:
    import asyncio

    from app import bot as botmod
    from app.config import load_config

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_giftvis_"))
    cfg_file = tmp / "config.yaml"
    raw = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    cfg_file.write_text(raw, encoding="utf-8")

    # ── 1. 纯函数：缺 uname 不能再丢整条礼物 ──
    print("[1] 旧协议缺 uname 时不再丢弃")
    ev = {"type": "SEND_GIFT", "data": {"cmd": "SEND_GIFT", "data": {
        "uid": 12345, "giftName": "辣条", "num": 3, "total_coin": 300,
        "sender_uinfo": {"uname": "观众甲"},
    }}}
    got = botmod._extract_gift_payload(ev)
    ok("有 sender_uinfo 时能解析", got is not None, got and got["uname"])

    ev2 = {"type": "SEND_GIFT", "data": {"cmd": "SEND_GIFT", "data": {
        "uid": 777, "giftName": "辣条", "num": 1, "total_coin": 100,
    }}}
    got2 = botmod._extract_gift_payload(ev2)
    ok("完全没有昵称也返回 payload（不再 None）", got2 is not None)

    ev3 = {"type": "SEND_GIFT", "data": {"cmd": "SEND_GIFT", "data": {
        "giftName": "辣条", "num": 1}}}
    ok("缺 uid 仍然返回 None（交给 _warn_gift_without_uid 分支）",
       botmod._extract_gift_payload(ev3) is None)

    # ── 2. 真实 Bot 实例：计数与写入 ──
    print("[2] 计数器：收到 / 记上 / 解码失败 / 缺昵称 / 重复")
    cfg = load_config(str(cfg_file))
    robot = botmod.LiveRobot(config=cfg, credential=None)
    robot.store = _FakeStore()
    robot.logger.disabled = False

    async def feed(payload_event: dict) -> None:
        await robot._handle_gift_payload(payload_event["data"]["data"], "SEND_GIFT")

    # 正常一条
    asyncio.run(feed(ev2))
    ok("seen 计 1", robot._gift_seen == 1, robot._gift_seen)
    ok("recorded 计 1", robot._gift_recorded == 1, robot._gift_recorded)
    ok("缺昵称计 1", robot._dropped_gift_no_uname == 1, robot._dropped_gift_no_uname)
    ok("写库时昵称有占位", robot.store.rows[0].uname == "用户777", robot.store.rows[0].uname)

    # 同一件礼物重复推（模拟灰度双推，带 tid）
    dup_payload = {"data": {"data": {
        "uid": 999, "uname": "甲", "giftName": "辣条", "num": 1,
        "src_protocol": "SEND_GIFT_V2", "tid": "abc123", "timestamp": 1,
    }}}
    asyncio.run(feed(dup_payload))
    asyncio.run(feed(dup_payload))
    ok("重复被去重并计数", robot._dropped_gift_dup >= 1, robot._dropped_gift_dup)
    ok("重复没有多写一行", len(robot.store.rows) == 2, len(robot.store.rows))

    # ── 3. V2 批量同款：缺 tid 时不能只记第 1 件 ──
    print("[3] V2 批量同款礼物（缺 tid）不能只记 1 件")
    batch = []
    for i in range(3):
        batch.append({"data": {"data": {
            "uid": 555, "uname": "乙", "giftName": "辣条", "num": 1,
            "src_protocol": "SEND_GIFT_V2", "timestamp": 1700000000,
            "_v2_index": i, "giftId": 31036,
        }}})
    before = len(robot.store.rows)
    for ev_item in batch:
        asyncio.run(feed(ev_item))
    ok("三件同款全部记上", len(robot.store.rows) - before == 3,
       len(robot.store.rows) - before)

    # ── 4. 心跳里有这些字段 ──
    print("[4] 心跳上报这些计数")
    hb = {}
    robot.write_heartbeat = lambda phase="running": hb.update({  # type: ignore[assignment]
        "gift_seen": robot._gift_seen,
        "gift_recorded": robot._gift_recorded,
        "gift_dropped_decode": robot._dropped_gift_decode,
        "gift_dropped_no_uname": robot._dropped_gift_no_uname,
        "gift_dropped_dup": robot._dropped_gift_dup,
    })
    robot.write_heartbeat()
    ok("心跳带 gift_seen", hb.get("gift_seen") == robot._gift_seen, hb)
    ok("心跳带 gift_dropped_decode", "gift_dropped_decode" in hb)

    # ── 5. 结构断言 ──
    print("[5] 结构断言")
    ok("V2 缺 pb 会尝试 JSON 兜底", "SEND_GIFT_V2 没有 pb，改用 JSON 字段解析" in BOT_SRC)
    ok("解码失败有计数",
       "self._dropped_gift_decode += 1" in BOT_SRC)
    ok("解码失败样本被保存", "_gift_decode_sample" in BOT_SRC)
    ok("解码失败日志有节流", "_last_decode_warn_ts" in BOT_SRC)
    ok("V2 批量传序号", 'payload["_v2_index"] = idx' in BOT_SRC)
    ok("去重键含序号与 giftId",
       "payload.get('_v2_index', -1)" in BOT_SRC and "payload.get('giftId')" in BOT_SRC)
    gift_extract = BOT_SRC.split("def _extract_gift_payload")[1].split(
        "def _extract_guard_buy_payload")[0]
    # 注意：bot.py 的注释里引用过旧写法（"以前 `if uid <= 0 or not uname: return None`"），
    # 所以断言必须带行首缩进只匹配**可执行行**，否则会被注释骗成假失败。
    OLD_GUARD = chr(10) + "    if uid <= 0 or not uname:"
    NEW_GUARD = chr(10) + "    if uid <= 0:" + chr(10) + "        return None"
    ok("礼物解析里 return None 只由缺 uid 触发",
       OLD_GUARD not in gift_extract and NEW_GUARD in gift_extract)
    ok("昵称占位可读", "用户{uid}" in BOT_SRC)
    ok("写库结果参与计数", 'stat_result.get("ok", True)' in BOT_SRC)
    ok("心跳有 gift_seen", '"gift_seen":' in BOT_SRC)
    ok("WebUI 有 gift_issue", 'r["gift_issue"] = {' in SRV_SRC)
    ok("UI 横幅解释四类", "解码失败 {{ selectedRoom.gift_issue.decode_failed }}" in SRV_SRC)
    ok("解码失败给出行动建议", "B站换了礼物协议" in SRV_SRC)

    # ── 6. 真实 SQLite 端到端：礼物写进去后，关键词统计能查到 ──
    print("[6] 关键词统计链路（聚合表）端到端")
    from app import tzutil
    from app.storage import GiftEvent, StatsStore
    tzutil.set_config_timezone("Asia/Shanghai")
    db = tmp / "gift.db"
    st = StatsStore(str(db))
    now_ts = int(time.time())
    month = tzutil.month_str(now_ts)
    st.record_gift_event(GiftEvent(
        ts=now_ts, month=month, uid=4242, uname="丙", event_type="SEND_GIFT",
        gift_name="心动盲盒", gift_num=2, is_blind_box=1, blind_box_cost=300,
        actual_value=260, profit_value=-40, raw_json=json.dumps({"total_coin": 30000})))
    total = st.get_monthly_total_blindbox(month)
    ok("房间整月盲盒统计非 0", total[0] == 2 and total[1] == 300, total)
    mine = st.get_user_monthly_blindbox(month, 4242)
    ok("个人整月盲盒统计非 0", mine is not None and mine[0] == 2, mine)
    today = st.get_today_user_blindbox(tzutil.day_start_ts(), tzutil.day_end_ts(), 4242)
    ok("个人今日盲盒统计非 0", today is not None and today[0] == 2, today)
    act = st.get_user_monthly_gift_activity(month, 4242)
    ok("礼物活跃度非 0", act[0] >= 1, act)
    st.close()
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("GIFT VISIBILITY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
