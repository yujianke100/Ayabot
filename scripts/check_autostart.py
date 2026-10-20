"""启动自动恢复验证：exe 打开后房间应自己起来（不发任何弹幕）。

覆盖三条路径：
  1. desired_states.json 里 true → WebUI 启动后自动拉起房间，心跳出现且弹幕已连接
  2. 旧版 running_rooms.json（托盘退出快照）能被迁移成期望状态
  3. 「僵尸 Task」——Task 建在别处已关闭的事件循环上——必须判为未运行，
     否则会顶掉真正的自动启动（这是「打开软件房间没起来」的根因）

用法: ./.venv/Scripts/python.exe scripts/check_autostart.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/autostart_check")
ROOM = "22491717"  # 会被 _pick_live_room() 覆盖成当前正在直播的房间
CRED = ROOT / "accounts" / "3706957229722452" / "credential.json"


def _pick_live_room() -> str:
    """挑一个当前正在直播的房间（弹幕只在开播时才连，离线房间测不出来）。"""
    global ROOM
    import urllib.request

    try:
        req = urllib.request.Request(
            "https://api.live.bilibili.com/room/v1/room/get_user_recommend?page=1&page_size=10",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/"},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        for r in data.get("data") or []:
            rid = str(r.get("roomid") or "")
            if rid.isdigit():
                ROOM = rid
                print(f"[选房] 使用正在直播的房间 {ROOM}（{(r.get('title') or '')[:20]}）")
                return ROOM
    except Exception as exc:  # noqa: BLE001
        print(f"[选房] 拉推荐房间失败，用默认 {ROOM}: {exc}")
    return ROOM


def build_deploy() -> str:
    """搭一个和 exe 一样的部署目录：config.yaml 在根，房间在 rooms/<id>/。"""
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    (SCRATCH / "data").mkdir(parents=True, exist_ok=True)
    room_dir = SCRATCH / "rooms" / ROOM
    (room_dir / "data").mkdir(parents=True, exist_ok=True)

    src = yaml.safe_load((ROOT / "rooms" / "1946287911" / "config.yaml").read_text(encoding="utf-8"))
    src["room_display_id"] = int(ROOM)
    src["account_uid"] = ""
    src["storage"] = {"sqlite_path": "data/bot.db"}
    src["web_ui"] = dict(src.get("web_ui") or {}, username="auto", password="auto123", api_token="")
    src["auth"] = {
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
        src.setdefault("features", {})[key] = False
    src["features"]["anchor_exclusive_reply"] = []
    src["features"]["keyword_rules"] = []
    src["features"]["keyword_filter"] = []
    src["runtime"] = dict(src.get("runtime") or {}, log_level="INFO", poll_interval_seconds=15)
    src["llm"] = dict(src.get("llm") or {}, enabled=False)
    (room_dir / "config.yaml").write_text(
        yaml.safe_dump(src, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    shutil.copy2(CRED, SCRATCH / "credential.json")
    # 顶层配置（exe 的 DATA_DIR/config.yaml）
    (SCRATCH / "config.yaml").write_text(
        yaml.safe_dump(src, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return str(SCRATCH / "config.yaml")


def scenario_zombie(failures: list[str]) -> None:
    """僵尸 Task：建在已关闭的事件循环上，必须判为未运行。"""
    import app.process_manager as pm

    async def _noop() -> None:
        await asyncio.sleep(999)

    loop = asyncio.new_event_loop()
    task = loop.create_task(_noop())
    pm._inproc_bots[ROOM] = ({"robot": None}, None, task)
    loop.close()

    if pm._inproc_is_running(ROOM):
        failures.append("僵尸 Task（事件循环已关）被判成 running")
    if ROOM in pm._inproc_bots:
        failures.append("僵尸 Task 没有被清理，仍留在 _inproc_bots")
    print(f"[僵尸] _inproc_is_running -> {pm._inproc_is_running(ROOM)}（应为 False）")


def scenario_legacy_migration(failures: list[str]) -> None:
    """只有 running_rooms.json 的老用户，应迁移成 desired_states.json。"""
    from app.web import server as srv

    legacy = SCRATCH / "running_rooms.json"
    legacy.write_text(json.dumps([ROOM]), encoding="utf-8")
    desired = SCRATCH / "data" / "desired_states.json"
    desired.unlink(missing_ok=True)

    old_base = srv._ROOMS_BASE_DIR
    srv._ROOMS_BASE_DIR = str(SCRATCH)
    try:
        states = srv._load_desired_states()
        print(f"[迁移] running_rooms.json -> desired states: {states}")
        if states.get(ROOM) is not True:
            failures.append(f"未从 running_rooms.json 迁移出期望状态: {states}")
        if not desired.exists():
            failures.append("迁移后没有落盘 desired_states.json")
    finally:
        srv._ROOMS_BASE_DIR = old_base


def scenario_heartbeat_bootstrap(failures: list[str]) -> None:
    """没有任何状态文件时，应能根据心跳 phase=running 推断出该跑的房间。"""
    from app.web import server as srv

    desired = SCRATCH / "data" / "desired_states.json"
    desired.unlink(missing_ok=True)
    (SCRATCH / "running_rooms.json").unlink(missing_ok=True)
    # 再补一个「用户主动停过」的房间，不能被拉起来
    stopped_room = SCRATCH / "rooms" / "999002"
    (stopped_room / "data").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRATCH / "rooms" / ROOM / "config.yaml", stopped_room / "config.yaml")
    (stopped_room / "data" / "bot_status.json").write_text(
        json.dumps({"ts": int(time.time()), "phase": "stopped"}), encoding="utf-8"
    )
    (SCRATCH / "rooms" / ROOM / "data" / "bot_status.json").write_text(
        json.dumps({"ts": int(time.time()), "phase": "running"}), encoding="utf-8"
    )

    old_base = srv._ROOMS_BASE_DIR
    srv._ROOMS_BASE_DIR = str(SCRATCH)
    try:
        states = srv._load_desired_states()
        print(f"[心跳推断] -> {states}")
        if states.get(ROOM) is not True:
            failures.append(f"心跳 phase=running 未推断为期望运行: {states}")
        if states.get("999002"):
            failures.append("心跳 phase=stopped 的房间被误判成要运行")
    finally:
        srv._ROOMS_BASE_DIR = old_base
        shutil.rmtree(stopped_room, ignore_errors=True)


def scenario_autostart(failures: list[str]) -> None:
    """真起 WebUI，验证房间被自动拉起并写出「已连弹幕」的心跳。"""
    import uvicorn

    from app.bot import LiveRobot
    from app.config import load_config
    from app.web import server as srv

    sent: list[str] = []

    async def _no_send(self, *, text: str, reply_uid=None) -> None:  # noqa: ANN001
        sent.append(text)

    LiveRobot._enqueue_message = _no_send  # type: ignore[assignment]

    cfg_path = str(SCRATCH / "config.yaml")
    # 清掉前面场景造的心跳，否则「有心跳」可能是假的
    (SCRATCH / "rooms" / ROOM / "data" / "bot_status.json").unlink(missing_ok=True)
    # 场景 1：desired_states.json 显式 true（正常用户点过启动后的状态）
    (SCRATCH / "running_rooms.json").unlink(missing_ok=True)
    (SCRATCH / "data" / "desired_states.json").write_text(
        json.dumps({ROOM: True}), encoding="utf-8"
    )

    srv.init_app(load_config(cfg_path), config_path=cfg_path)

    config = uvicorn.Config(srv.app, host="127.0.0.1", port=19894, log_level="warning",
                            log_config=None)
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True, name="uvicorn-check")
    th.start()

    hb_path = SCRATCH / "rooms" / ROOM / "data" / "bot_status.json"
    hb: dict = {}
    deadline = time.time() + 90
    while time.time() < deadline:
        if hb_path.exists():
            try:
                hb = json.loads(hb_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                hb = {}
            if hb.get("danmaku_connected"):
                break
        time.sleep(1)

    print(f"[自动启动] 心跳: {json.dumps(hb, ensure_ascii=False)}")
    if not hb:
        failures.append("启动 90s 内没有生成心跳，房间没被自动拉起")
    else:
        if hb.get("phase") in (None, "stopped"):
            failures.append(f"心跳 phase={hb.get('phase')}，房间没真正跑")
        if not hb.get("danmaku_connected"):
            if hb.get("live"):
                failures.append("房间在播但 danmaku_connected 不是 True")
            else:
                print("[自动启动] 房间没在播（live=false），跳过弹幕连接断言")
        if hb.get("credential_status") != "ok":
            failures.append(f"凭证状态异常: {hb.get('credential_status')}")

    import app.process_manager as pm
    print(f"[自动启动] room_status -> {pm.room_status(ROOM)}")
    if pm.room_status(ROOM) != "running":
        failures.append("自动启动后 room_status 不是 running")

    server.should_exit = True
    th.join(timeout=15)
    if sent:
        failures.append(f"测试期间发出了 {len(sent)} 条消息（应当为 0）")


def main() -> int:
    if not CRED.exists():
        print("缺少本地凭证，无法验证（需要 accounts/<uid>/credential.json）")
        return 2
    sys.frozen = True  # 模拟 exe
    failures: list[str] = []
    try:
        _pick_live_room()
        build_deploy()
        scenario_zombie(failures)
        scenario_legacy_migration(failures)
        scenario_heartbeat_bootstrap(failures)
        scenario_autostart(failures)
    finally:
        sys.frozen = False

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nAUTOSTART OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
