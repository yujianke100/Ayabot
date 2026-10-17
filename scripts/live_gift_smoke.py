"""端到端验证：真实凭证 + 真实直播间 + 真实 SEND_GIFT_V2 事件 → 入库。

发送类功能全部被 monkeypatch 拦掉（绝不向别人的直播间发弹幕），
只验证「收到事件 → 解码 → 入库 → 月聚合」这条链路。

用法: ./.venv/Scripts/python.exe scripts/live_gift_smoke.py <room_display_id> <anchor_uid> [秒数]
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/live_smoke")
CRED = ROOT / "accounts" / "3706957229722452" / "credential.json"


def build_config(room_id: int, anchor_uid: int) -> str:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load((ROOT / "rooms" / "1946287911" / "config.yaml").read_text(encoding="utf-8"))
    base["room_display_id"] = room_id
    base["anchor_uid"] = anchor_uid
    # 不写回真实 accounts/ 凭证：用一份拷贝作为 credential_store_path
    cred_copy = SCRATCH / "credential.json"
    shutil.copy2(CRED, cred_copy)
    base["account_uid"] = ""
    base["storage"] = {"sqlite_path": "bot.db"}
    base["auth"] = {
        "auto_login": True,
        "credential_store_path": str(cred_copy),
        "qr_poll_seconds": 1.0,
        "refresh_interval_seconds": 3600,
        "wait_for_login": True,
    }
    feat = base.setdefault("features", {})
    for key in (
        "thanks_enabled", "welcome_enabled", "guard_thanks_enabled", "like_thanks_enabled",
        "share_thanks_enabled", "follow_thanks_enabled", "periodic_message_enabled",
        "connected_message_enabled", "blindbox_enabled",
    ):
        feat[key] = False
    feat["anchor_exclusive_reply"] = []
    feat["keyword_rules"] = []
    feat["keyword_filter"] = []
    base["rate_limit"] = dict(base.get("rate_limit") or {}, send_interval_seconds=1.0)
    base["runtime"] = dict(base.get("runtime") or {}, log_level="DEBUG", poll_interval_seconds=15)
    base["llm"] = dict(base.get("llm") or {}, enabled=False)
    path = SCRATCH / "config.yaml"
    path.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(path)


async def main(room_id: int, anchor_uid: int, seconds: int) -> int:
    from app.auth import AuthManager
    from app.bot import LiveRobot
    from app.config import load_config

    db_path = SCRATCH / "bot.db"
    if db_path.exists():
        db_path.unlink()

    config = load_config(build_config(room_id, anchor_uid))
    auth = AuthManager(config)
    credential = await auth.prepare_credential()
    print("credential ready; status =", auth.load_status())

    robot = LiveRobot(config=config, credential=credential, auth=auth)

    # 绝不对真实直播间发言：所有出站消息只记录
    outbound: list[str] = []

    async def _no_send(*, text: str, reply_uid=None) -> None:  # noqa: ANN001
        outbound.append(text)

    robot._enqueue_message = _no_send  # type: ignore[assignment]

    task = asyncio.create_task(robot.run())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    hb = json.loads((SCRATCH / "bot_status.json").read_text(encoding="utf-8"))
    print("heartbeat:", json.dumps(hb, ensure_ascii=False))
    print("outbound messages (should be 0):", len(outbound))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) c FROM gift_events").fetchone()["c"]
    print("gift_events rows:", total)
    for row in conn.execute(
        "SELECT ts,uid,uname,gift_name,gift_num,is_blind_box,blind_box_cost,actual_value,"
        "profit_value,json_extract(raw_json,'$.src_protocol') proto "
        "FROM gift_events ORDER BY id DESC LIMIT 8"
    ):
        print("  ", time.strftime("%H:%M:%S", time.localtime(row["ts"])), dict(row))
    conn.close()

    ok = hb.get("danmaku_connected") or total > 0
    if not ok:
        print("\nFAILED: 没连上弹幕，也没有礼物入库")
        return 1
    if total == 0:
        print("\nWARN: 连上了但窗口内房间没人送礼物（无法验证入库）；建议换房间/加时长")
        return 2
    print("\nLIVE SMOKE OK")
    return 0


if __name__ == "__main__":
    room = int(sys.argv[1])
    uid = int(sys.argv[2])
    secs = int(sys.argv[3]) if len(sys.argv) > 3 else 240
    raise SystemExit(asyncio.run(main(room, uid, secs)))
