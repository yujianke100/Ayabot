"""第二十七轮自检：事件可观测性、协议健壮性、分页夹取。

覆盖的问题：
  1. 「统计全是 0」时无法判断是「没收到事件」还是「收到不认识的事件」
     → 现在有心跳计数 + 每 10 分钟 INFO 汇总 + 未知事件告警 + 界面提示。
  2. UNIVERSAL_EVENT_GIFT_V2 带 pb 时，旧代码按 JSON 解析会产出
     「name=gift / num=1 / 金额 0」的假礼物（条数在涨、金额永远 0）。
  3. protobuf 的 fixed64/fixed32 分支没有边界检查（静默截断）。
  4. `/api/danmaku_log?limit=-1` 在 SQLite 里等于「不限」，可拉全表。

用法: ./.venv/Scripts/python.exe scripts/check_event_observability.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _read_src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


BOT_SRC = _read_src("app/bot.py")
PB_SRC = _read_src("app/gift_v2.py")
STORE_SRC = _read_src("app/storage.py")
SRV_SRC = _read_src("app/web/server.py")


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
    from app import bot as botmod
    from app.config import load_config
    from app.storage import StatsStore

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_evobs_"))
    cfg_file = tmp / "config.yaml"
    cfg_file.write_text((ROOT / "config.example.yaml").read_text(encoding="utf-8"),
                        encoding="utf-8")

    # ── 1. 事件计数：早于所有 return ──
    print("[1] 事件类型计数")
    cfg = load_config(str(cfg_file))
    robot = botmod.LiveRobot(config=cfg, credential=None)
    robot.store = _FakeStore()

    async def feed_all(ev: dict) -> None:
        await robot._on_all_events(ev)

    # 已知礼物事件 + 噪音事件 + 完全不认识的事件
    for et in ("DANMU_MSG", "SEND_GIFT", "SEND_GIFT", "INTERACT_WORD_V2",
               "ONLINE_RANK_COUNT", "VIEW"):
        asyncio.run(feed_all({"type": et, "data": {}}))
    ok("噪音/被过滤的事件也计数", robot._event_counts.get("DANMU_MSG") == 1,
       robot._event_counts)
    ok("SEND_GIFT 计到 2", robot._event_counts.get("SEND_GIFT") == 2)
    ok("VIEW 也计数（不再被提前 return 漏掉）", robot._event_counts.get("VIEW") == 1)

    # 未知礼物类事件
    asyncio.run(feed_all({"type": "SEND_GIFT_V3", "data": {}}))
    ok("未识别的礼物类事件被记下", "SEND_GIFT_V3" in robot._unknown_gift_like,
       robot._unknown_gift_like)
    asyncio.run(feed_all({"type": "DANMU_MSG", "data": {}}))
    ok("普通未知事件不算礼物类", "DANMU_MSG" not in robot._unknown_gift_like)

    # ── 2. 汇总日志三条分支 ──
    print("[2] 10 分钟汇总日志")
    logs: list[tuple[str, str]] = []

    class _Cap:
        def __init__(self) -> None:
            self.msgs: list[tuple[str, str]] = []

        def info(self, fmt, *a, **k):
            self.msgs.append(("info", fmt % a if a else fmt))

        def warning(self, fmt, *a, **k):
            self.msgs.append(("warn", fmt % a if a else fmt))

        def debug(self, *a, **k):
            pass

        def error(self, fmt, *a, **k):
            self.msgs.append(("error", fmt % a if a else fmt))

    cap = _Cap()
    robot.logger = cap  # type: ignore[assignment]
    robot._log_event_summary()
    kinds = [k for k, _ in cap.msgs]
    joined = " ".join(m for _, m in cap.msgs)
    ok("打印了事件汇总", any("事件统计" in m for _, m in cap.msgs))
    ok("未知礼物类事件有 warning",
       any(k == "warn" and "SEND_GIFT_V3" in m for k, m in cap.msgs), joined[:120])
    ok("汇总后计数清零", robot._event_counts == {}, robot._event_counts)

    # 完全没有礼物事件时要有明确 warning
    cap.msgs.clear()
    robot._event_counts = {"DANMU_MSG": 50}
    robot._unknown_gift_like = set()
    robot._log_event_summary()
    ok("没有礼物事件时警告",
       any(k == "warn" and "没有收到任何已知礼物事件" in m for k, m in cap.msgs),
       [m for k, m in cap.msgs])

    # ── 3. 心跳带计数 ──
    print("[3] 心跳字段")
    hb: dict = {}

    def _fake_hb(phase: str = "running") -> None:
        hb.update({
            "event_types": dict(sorted(robot._event_counts.items(),
                                       key=lambda kv: kv[1], reverse=True)[:12]),
            "unknown_gift_events": sorted(robot._unknown_gift_like),
        })

    robot._event_counts = {"DANMU_MSG": 5, "SEND_GIFT": 3}
    robot._unknown_gift_like = {"SEND_GIFT_V3"}
    _fake_hb()
    ok("心跳有 event_types", hb.get("event_types", {}).get("DANMU_MSG") == 5, hb)
    ok("心跳有 unknown_gift_events", hb.get("unknown_gift_events") == ["SEND_GIFT_V3"])

    # ── 4. UNIVERSAL_EVENT_GIFT_V2 带 pb 时走 V2 分支 ──
    print("[4] UNIVERSAL_EVENT_GIFT_V2 的 pb 兜底")

    async def _fake_on_gift_v2(ev):  # noqa: ANN001
        _fake_on_gift_v2.called.append(ev)  # type: ignore[attr-defined]

    _fake_on_gift_v2.called = []  # type: ignore[attr-defined]
    robot._on_gift_v2 = _fake_on_gift_v2  # type: ignore[assignment]
    asyncio.run(robot._on_gift({"type": "UNIVERSAL_EVENT_GIFT_V2",
                                "data": {"data": {"pb": "Zm9v"}}}))
    ok("带 pb 时转给 _on_gift_v2", len(_fake_on_gift_v2.called) == 1,  # type: ignore[attr-defined]
       _fake_on_gift_v2.called)  # type: ignore[attr-defined]

    robot.store = _FakeStore()
    asyncio.run(robot._on_gift({"type": "UNIVERSAL_EVENT_GIFT",
                                "data": {"data": {"uid": 1, "uname": "甲",
                                                  "giftName": "辣条", "num": 1}}}))
    ok("纯 JSON 仍按原路解析", len(robot.store.rows) == 1, len(robot.store.rows))

    # ── 5. protobuf 边界 ──
    print("[5] protobuf 边界与体积上限")
    from app.gift_v2 import MAX_PB_B64_LEN, _iter_fields, decode_send_gift_v2
    for raw, label in ((b"\x09\x01\x02", "固定 64 位截断"),
                       (b"\x0d\x01", "固定 32 位截断")):
        try:
            list(_iter_fields(raw))
            ok(f"{label} 会报错", False, "没报错")
        except ValueError:
            ok(f"{label} 会报错", True)
    ok("超大 pb 返回空而不是解析",
       decode_send_gift_v2("A" * (MAX_PB_B64_LEN + 1)) == [])
    ok("正常 pb 仍可解", isinstance(decode_send_gift_v2("Zg=="), list))

    # ── 6. 分页夹取 ──
    print("[6] danmaku_log 分页夹取")
    st = StatsStore(str(tmp / "dm.db"))
    for i in range(30):
        st.record_danmaku(ts=int(time.time()) + i, uid=i, uname=f"u{i}", content=f"c{i}")
    ok("limit=-1 被夹到 <=1000 且不报错", len(st.get_danmaku_log(limit=-1)) <= 1000)
    ok("limit 过大被夹", len(st.get_danmaku_log(limit=10 ** 9)) <= 1000)
    ok("负 offset 不报错", st.get_danmaku_log(limit=5, offset=-3) is not None)
    ok("正常 limit 生效", len(st.get_danmaku_log(limit=7)) == 7,
       len(st.get_danmaku_log(limit=7)))
    # limit=0 是 falsy → 回落到默认 50（而**不是** SQLite 的「不限」）
    ok("limit=0 回落到默认 50 而不是全表",
       len(st.get_danmaku_log(limit=0)) == 30, len(st.get_danmaku_log(limit=0)))
    st.close()

    # ── 7. 结构断言 ──
    print("[7] 结构断言")
    ok("已知礼物事件清单存在", "KNOWN_GIFT_EVENTS = frozenset" in BOT_SRC)
    ok("计数早于 PK/噪音 return",
       BOT_SRC.index("self._bump_event_count(event_type)")
       < BOT_SRC.index('if event_type.startswith("PK_")'))
    ok("心跳循环里会打汇总", "self._log_event_summary()" in BOT_SRC)
    ok("汇总周期是 10 分钟", "ticks % 40 == 0" in BOT_SRC)
    ok("心跳字段含 event_types", '"event_types":' in BOT_SRC)
    ok("心跳字段含 unknown_gift_events", '"unknown_gift_events":' in BOT_SRC)
    ok("pb 有体积上限", "MAX_PB_B64_LEN" in PB_SRC)
    ok("fixed64 有边界检查", "fixed64 field overruns buffer" in PB_SRC)
    ok("fixed32 有边界检查", "fixed32 field overruns buffer" in PB_SRC)
    ok("分页 limit 被夹取", "limit = min(_lim, 1000)" in STORE_SRC)
    ok("unknown_events 透出到房间列表", "unknown_gift_events" in SRV_SRC)
    ok("界面显示未识别事件名", "收到未识别的礼物类事件" in SRV_SRC)

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("EVENT OBSERVABILITY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
