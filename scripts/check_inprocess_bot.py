"""冻结模式（exe / 同进程 Bot）验证：启动→心跳→停止，全程不发弹幕。

exe 用户走的是 process_manager 的同进程分支（sys.frozen=True），
与子进程模式是两套代码，必须单独验证。

用法: ./.venv/Scripts/python.exe scripts/check_inprocess_bot.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/inproc_test")
ROOM = "24872476"
CRED = ROOT / "accounts" / "3706957229722452" / "credential.json"
RUN_SECONDS = 30


def build_room() -> None:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    room_dir = SCRATCH / "rooms" / ROOM
    (room_dir / "data").mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_load((ROOT / "rooms" / "1946287911" / "config.yaml").read_text(encoding="utf-8"))
    cfg["room_display_id"] = int(ROOM)
    cfg["account_uid"] = ""
    cfg["storage"] = {"sqlite_path": "data/bot.db"}
    cfg["auth"] = {
        "auto_login": True,
        "credential_store_path": str(SCRATCH / "credential.json"),
        "qr_poll_seconds": 1.0,
        "refresh_interval_seconds": 3600,
        "wait_for_login": True,
    }
    for key in (
        "thanks_enabled", "welcome_enabled", "guard_thanks_enabled", "like_thanks_enabled",
        "share_thanks_enabled", "follow_thanks_enabled", "periodic_message_enabled",
        "connected_message_enabled", "blindbox_enabled",
    ):
        cfg.setdefault("features", {})[key] = False
    cfg["features"]["anchor_exclusive_reply"] = []
    cfg["features"]["keyword_rules"] = []
    cfg["features"]["keyword_filter"] = []
    cfg["runtime"] = dict(cfg.get("runtime") or {}, log_level="INFO", poll_interval_seconds=15)
    cfg["llm"] = dict(cfg.get("llm") or {}, enabled=False)
    (room_dir / "config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    shutil.copy2(CRED, SCRATCH / "credential.json")


async def main() -> int:
    from app import process_manager as pm
    from app.bot import LiveRobot

    build_room()
    pm.set_rooms_base_dir(SCRATCH)

    # 断言：非冻结模式走子进程，冻结模式走同进程
    frozen = getattr(sys, "frozen", False)
    print("before: sys.frozen =", frozen, "| _use_inprocess() =", pm._use_inprocess())

    # 阻止任何真实发言（测试连的是别人的直播间）
    sent: list[str] = []

    async def _no_send(self, *, text: str, reply_uid=None) -> None:  # noqa: ANN001
        sent.append(text)

    LiveRobot._enqueue_message = _no_send  # type: ignore[assignment]

    sys.frozen = True  # 模拟 exe
    failures: list[str] = []
    try:
        print("after: _use_inprocess() =", pm._use_inprocess())
        if not pm._use_inprocess():
            failures.append("sys.frozen=True 时未切到同进程模式")

        started_at = time.time()
        ok = await pm.start_room_async(ROOM)
        elapsed = time.time() - started_at
        print(f"start_room_async -> {ok} in {elapsed:.2f}s")
        if not ok:
            failures.append("同进程启动返回 False")
        if elapsed > 5:
            failures.append(f"启动被阻塞 {elapsed:.1f}s（exe 下会卡住 HTTP 请求）")

        if pm.room_status(ROOM) != "running":
            failures.append("启动后状态不是 running")

        hb_path = SCRATCH / "rooms" / ROOM / "data" / "bot_status.json"
        deadline = time.time() + 20
        hb: dict = {}
        while time.time() < deadline:
            if hb_path.exists():
                hb = json.loads(hb_path.read_text(encoding="utf-8"))
                if hb.get("danmaku_connected"):
                    break
            await asyncio.sleep(1)
        print("heartbeat:", json.dumps(hb, ensure_ascii=False))
        if not hb:
            failures.append("没有生成心跳文件")
        elif not hb.get("danmaku_connected"):
            failures.append("心跳里 danmaku_connected 不是 True（未连上弹幕）")
        if hb.get("credential_status") != "ok":
            failures.append(f"心跳凭证状态异常: {hb.get('credential_status')}")

        await asyncio.sleep(max(RUN_SECONDS - (time.time() - started_at), 5))

        stopped = await pm.stop_room_async(ROOM)
        print("stop_room_async ->", stopped, "| status =", pm.room_status(ROOM))
        if not stopped or pm.room_status(ROOM) == "running":
            failures.append("停止失败")
        final = json.loads(hb_path.read_text(encoding="utf-8"))
        print("final heartbeat phase:", final.get("phase"))
        if final.get("phase") != "stopped":
            failures.append(f"停止后心跳 phase={final.get('phase')}")
        if sent:
            failures.append(f"测试期间发出了 {len(sent)} 条消息（应当为 0）")
    finally:
        sys.frozen = False

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nIN-PROCESS (FROZEN) OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
