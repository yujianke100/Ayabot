"""礼物处理路径验证：感谢开关关闭仍入库、开启时发感谢、重复事件去重。

用法: ./.venv/Scripts/python.exe scripts/check_gift_handler.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/handler_test")


def build_config(thanks: bool) -> str:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load((ROOT / "rooms" / "1946287911" / "config.yaml").read_text(encoding="utf-8"))
    base["storage"] = {"sqlite_path": "bot.db"}
    base["auth"] = {"auto_login": True, "credential_store_path": "cred.json",
                    "qr_poll_seconds": 1.0, "refresh_interval_seconds": 3600}
    feat = base.setdefault("features", {})
    feat["thanks_enabled"] = thanks
    feat["thanks_template"] = "感谢 {uname} 的 {gift_name} x{gift_num}"
    feat["blindbox_enabled"] = True
    base["cooldown"] = dict(base.get("cooldown") or {}, thanks_user_seconds=0)
    path = SCRATCH / "config.yaml"
    path.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(path)


PAYLOAD = {
    "uid": 123456, "uname": "测试用户", "giftName": "爱心抱枕", "num": 1,
    "price": 16000, "total_coin": 15000, "tid": "tid-abc", "rnd": "rnd-abc",
    "timestamp": 1792219000, "action": "投喂", "src_protocol": "SEND_GIFT_V2",
    "blind_gift": {"original_gift_name": "心动盲盒", "original_gift_price": 15000,
                   "gift_tip_price": 16000},
}


async def run_case(thanks: bool) -> tuple[int, int, list[str]]:
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

    await robot._handle_gift_payload(dict(PAYLOAD), "SEND_GIFT")
    # 同一件礼物被新协议重复推送
    await robot._handle_gift_payload(dict(PAYLOAD), "SEND_GIFT")
    # 只有 uid 缺失的匿名事件，应被丢弃
    await robot._handle_gift_payload({**PAYLOAD, "uid": 0, "uname": "要***"}, "SEND_GIFT")

    import sqlite3
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT COUNT(*), SUM(blind_box_cost), SUM(actual_value) FROM gift_events").fetchone()
    conn.close()
    robot.store.close()
    return int(rows[0]), int(rows[1] or 0), sent


async def main() -> int:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    failures: list[str] = []

    count_off, cost_off, sent_off = await run_case(thanks=False)
    print(f"thanks=off -> rows={count_off} cost={cost_off} messages={sent_off}")
    if count_off != 1:
        failures.append(f"关闭感谢时仍应入库 1 行（去重后），实际 {count_off}")
    if cost_off != 150:
        failures.append(f"盲盒花费应为 150，实际 {cost_off}")
    if sent_off:
        failures.append("关闭感谢时不应发送消息")

    count_on, cost_on, sent_on = await run_case(thanks=True)
    print(f"thanks=on  -> rows={count_on} cost={cost_on} messages={sent_on}")
    if count_on != 1:
        failures.append(f"重复事件未去重，rows={count_on}")
    if not sent_on:
        failures.append("开启感谢时应当发送感谢消息")
    else:
        if "测试用户" not in sent_on[0] or "爱心抱枕" not in sent_on[0]:
            failures.append(f"感谢模板占位符未替换: {sent_on[0]}")
        if "+10电池" not in sent_on[0]:
            failures.append(f"盲盒盈亏未拼进感谢语: {sent_on[0]}")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nGIFT HANDLER OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
