"""上舰（大航海）链路自检：真实事件 → 解析 → 去重入库 → 文案。

样本是 2026-10 线上直播间抓到的同一次开通推送的三种事件
（GUARD_BUY / USER_TOAST_MSG / USER_TOAST_MSG_V2，uid+M 舰长，start_time 相同）。

用法: ./.venv/Scripts/python.exe scripts/check_guard_events.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/guard_test")
FIXTURES = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/events_multi.json")

# 三种真实事件的最小复刻（字段结构与抓包一致，昵称做脱敏）
REAL_UID = 2060993
START_TIME = 1792218866

GUARD_BUY = {
    "room_display_id": 22491717, "room_real_id": 22491717, "type": "GUARD_BUY",
    "data": {"cmd": "GUARD_BUY", "data": {
        "uid": REAL_UID, "username": "测试舰长", "guard_level": 3, "num": 1,
        "price": 198000, "gift_id": 10003, "gift_name": "舰长",
        "start_time": START_TIME, "end_time": START_TIME}},
}
USER_TOAST_MSG = {
    "room_display_id": 22491717, "room_real_id": 22491717, "type": "USER_TOAST_MSG",
    "data": {"cmd": "USER_TOAST_MSG", "data": {
        "uid": REAL_UID, "username": "测试舰长", "guard_level": 3, "num": 1,
        "price": 138000, "role_name": "舰长", "unit": "月", "gift_id": 10003,
        "start_time": START_TIME, "end_time": START_TIME,
        "toast_msg": "<%测试舰长%> 开通了舰长"}},
}
USER_TOAST_MSG_V2 = {
    "room_display_id": 22491717, "room_real_id": 22491717, "type": "USER_TOAST_MSG_V2",
    "data": {"cmd": "USER_TOAST_MSG_V2", "data": {
        "sender_uinfo": {"uid": REAL_UID, "base": {"name": "测试舰长", "face": ""}},
        "receiver_uinfo": {"uid": 304578055, "base": {"name": "主播", "face": ""}},
        "guard_info": {"guard_level": 3, "role_name": "舰长", "room_guard_count": 7151,
                       "op_type": 1, "start_time": START_TIME, "end_time": START_TIME},
        "pay_info": {"payflow_id": "x", "price": 138000, "num": 1, "unit": "月"},
        "gift_info": {"gift_id": 10003},
        "toast_msg": "<%测试舰长%> 开通了舰长"}},
}


def build_config(thanks: bool) -> str:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load((ROOT / "rooms" / "1946287911" / "config.yaml").read_text(encoding="utf-8"))
    base["storage"] = {"sqlite_path": "bot.db"}
    base["auth"] = {"auto_login": True, "credential_store_path": "cred.json",
                    "qr_poll_seconds": 1.0, "refresh_interval_seconds": 3600}
    feat = base["features"]
    feat["guard_thanks_enabled"] = thanks
    feat["guard_thanks_template_captain"] = "感谢{uname}上舰，{guard_name}x{months}喵~！"
    feat["guard_thanks_template_commander"] = "感谢{uname}提督x{months}"
    feat["guard_thanks_template_governor"] = "感谢{uname}总督x{months}"
    feat["guard_thanks_template_default"] = "感谢{uname}开通{guard_type}x{months}"
    base["cooldown"] = dict(base.get("cooldown") or {}, thanks_user_seconds=0)
    path = SCRATCH / "config.yaml"
    path.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(path)


async def run_case(thanks: bool) -> tuple[list[dict], list[str], dict]:
    from app.bot import LiveRobot
    from app.config import load_config

    db = SCRATCH / "bot.db"
    if db.exists():
        db.unlink()
    config = load_config(build_config(thanks))
    robot = LiveRobot(config=config)

    sent: list[str] = []

    async def collect(*, text: str, reply_uid=None) -> None:  # noqa: ANN001
        sent.append(text)

    robot._enqueue_message = collect  # type: ignore[assignment]

    # B站 同一次开通依次推送三种事件
    await robot._on_guard_buy(GUARD_BUY)
    await robot._on_guard_buy(USER_TOAST_MSG)
    await robot._on_guard_buy(USER_TOAST_MSG_V2)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM guard_events")]
    conn.close()
    robot.store.close()
    return rows, sent, {}


async def main() -> int:
    from app.bot import _extract_guard_buy_payload

    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    failures: list[str] = []

    # 1) 三种事件都要能解析（旧代码在这里全部返回 None）
    for name, ev in (("GUARD_BUY", GUARD_BUY), ("USER_TOAST_MSG", USER_TOAST_MSG),
                     ("USER_TOAST_MSG_V2", USER_TOAST_MSG_V2)):
        g = _extract_guard_buy_payload(ev)
        print(f"{name:20s} -> {g}")
        if g is None:
            failures.append(f"{name} 解析失败")
            continue
        if g["uid"] != REAL_UID or g["guard_level"] != 3 or g["guard_name"] != "舰长":
            failures.append(f"{name} 字段不对: {g}")

    # 2) 真实抓包样本也应当解析成功
    if FIXTURES.exists():
        fx = json.load(open(FIXTURES, encoding="utf-8"))
        for k in ("GUARD_BUY", "USER_TOAST_MSG", "USER_TOAST_MSG_V2"):
            g = _extract_guard_buy_payload(fx[k])
            if g is None:
                failures.append(f"真实抓包 {k} 解析失败")

    # 3) 三连事件只入库 1 条、只感谢 1 次
    rows, sent, _ = await run_case(thanks=True)
    print(f"\nthanks=on -> rows={len(rows)} sent={sent}")
    if len(rows) != 1:
        failures.append(f"三连事件入库 {len(rows)} 条（应为 1 条）")
    if len(sent) != 1:
        failures.append(f"三连事件发了 {len(sent)} 条感谢（应为 1 条）")
    if sent and ("感谢测试舰长上舰" not in sent[0] or "舰长x1" not in sent[0]):
        failures.append(f"感谢文案不对: {sent[0]}")
    if rows:
        r = rows[0]
        print("row:", {k: r[k] for k in ("uid", "uname", "guard_level", "guard_name",
                                          "months", "price_coin", "value", "start_time", "source")})
        if r["uname"] != "测试舰长" or r["guard_name"] != "舰长" or r["guard_level"] != 3:
            failures.append(f"入库字段不对: {r}")
        # 后到的实付价（138000）应当纠正先到的原价（198000）
        if r["price_coin"] != 138000 or r["value"] != 1380:
            failures.append(f"实付价未纠正: price_coin={r['price_coin']} value={r['value']}")

    # 4) 关闭致谢仍要记录
    rows2, sent2, _ = await run_case(thanks=False)
    print(f"thanks=off -> rows={len(rows2)} sent={sent2}")
    if len(rows2) != 1:
        failures.append(f"关闭致谢时未记录（{len(rows2)} 条）")
    if sent2:
        failures.append("关闭致谢时不应发消息")

    # 5) 同一用户再开一次（不同 start_time）应新增一条
    from app.bot import LiveRobot  # noqa: PLC0415
    from app.config import load_config  # noqa: PLC0415

    config = load_config(build_config(True))
    robot = LiveRobot(config=config)
    robot._enqueue_message = lambda **kw: asyncio.sleep(0)  # type: ignore[assignment]
    second = json.loads(json.dumps(GUARD_BUY))
    second["data"]["data"]["start_time"] = START_TIME + 30 * 86400
    await robot._on_guard_buy(second)
    rows3 = robot.store.list_guard_events()
    totals = robot.store.get_guard_totals()
    robot.store.close()
    print(f"第二次开通 -> rows={len(rows3)} totals={totals}")
    if len(rows3) != 2:
        failures.append(f"续费未新增记录（{len(rows3)} 条）")
    if totals["count"] != 2 or totals["captain"] != 2:
        failures.append(f"汇总不对: {totals}")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nGUARD EVENTS OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
