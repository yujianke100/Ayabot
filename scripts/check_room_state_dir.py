"""房间「状态文件目录」口径一致性自检。

背景：机器人侧把心跳 / 凭证状态 / 崩溃记录写在
`Path(config.storage.sqlite_path).parent` 下，而 WebUI 曾经写死
`<rooms>/<rid>/data/`。默认配置下两者恰好相同，所以这个 bug 只在
「改过 sqlite_path」的房间上发作；一旦发作，用户看到的正是
「机器人莫名其妙一直重启」（守护进程永远读不到心跳 → 判僵尸）
和「凭证失效不提示重新登录」（永远读不到 auth_status.json）。

本脚本断言：**同一个房间，四类状态文件在两侧算出的路径必须完全相同**，
并且默认配置下仍然是 `<room>/data`（不能因为修复而搬家）。

    ./.venv/Scripts/python.exe scripts/check_room_state_dir.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROOM_DEFAULT = "1000000001"
ROOM_CUSTOM = "1000000002"
STATE_FILES = ("bot_status.json", "auth_status.json", "crash_info.json", "bot_error.json")


def main() -> int:
    from app.auth import AUTH_STATUS_FILE, AuthManager
    from app.bot import LiveRobot
    from app.config import HEARTBEAT_FILE, load_config
    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_state_dir_"))
    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert 'sqlite_path: "data/bot.db"' in example, "config.example.yaml 结构变了，需要更新本脚本"

    # 房间 A：默认路径；房间 B：把库放进 data/stats/ 子目录（父目录不再是 data）
    for rid, rel in ((ROOM_DEFAULT, "data/bot.db"), (ROOM_CUSTOM, "data/stats/bot.db")):
        d = tmp / "rooms" / rid
        (d / "data").mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(
            example.replace('sqlite_path: "data/bot.db"', f'sqlite_path: "{rel}"'),
            encoding="utf-8")

    srv._ROOMS_BASE_DIR = str(tmp)
    srv._ROOM_DB_PATH_CACHE.clear()

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    try:
        print("[1] 默认路径：状态文件仍在 <room>/data 下，行为不变")
        for name in STATE_FILES:
            got = srv._room_data_file(ROOM_DEFAULT, name)
            want = tmp / "rooms" / ROOM_DEFAULT / "data" / name
            ok(f"{name} 路径未搬家", got == want, str(got))

        print("[2] 自定义 sqlite_path：WebUI 必须跟着库走")
        for name in STATE_FILES:
            got = srv._room_data_file(ROOM_CUSTOM, name)
            want = tmp / "rooms" / ROOM_CUSTOM / "data" / "stats" / name
            ok(f"{name} 在库同目录", got == want, str(got))

        print("[3] 与机器人侧逐项对齐（这是真正的不变量）")
        cfg_d = load_config(str(tmp / "rooms" / ROOM_DEFAULT / "config.yaml"))
        cfg_c = load_config(str(tmp / "rooms" / ROOM_CUSTOM / "config.yaml"))
        for rid, cfg in ((ROOM_DEFAULT, cfg_d), (ROOM_CUSTOM, cfg_c)):
            bot_hb = LiveRobot.__dict__["_status_path"](type("B", (), {"config": cfg})())
            ok(f"[{rid}] 心跳：WebUI == 机器人",
               srv._room_data_file(rid, HEARTBEAT_FILE) == bot_hb,
               f"{srv._room_data_file(rid, HEARTBEAT_FILE)} vs {bot_hb}")

            am = AuthManager(cfg)
            ok(f"[{rid}] 凭证状态：WebUI == 机器人",
               srv._room_data_file(rid, AUTH_STATUS_FILE) == am.status_path(),
               f"{srv._room_data_file(rid, AUTH_STATUS_FILE)} vs {am.status_path()}")

            bot_err = Path(cfg.storage.sqlite_path).parent / "bot_error.json"
            ok(f"[{rid}] 崩溃原因：WebUI == 机器人",
               srv._room_data_file(rid, "bot_error.json") == bot_err,
               f"{srv._room_data_file(rid, 'bot_error.json')} vs {bot_err}")

            ok(f"[{rid}] 崩溃现象：WebUI == 机器人",
               srv._crash_info_path(rid) == Path(cfg.storage.sqlite_path).parent / "crash_info.json")

        print("[4] 端到端：写进自定义目录的心跳必须能被 /api/rooms 读到")
        hb_path = srv._room_data_file(ROOM_CUSTOM, HEARTBEAT_FILE)
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        hb_path.write_text(json.dumps({"ts": int(time.time()), "pid": 4321,
                                       "phase": "waiting_login", "waiting_login": True}),
                           encoding="utf-8")
        hb = srv._read_room_json(ROOM_CUSTOM, HEARTBEAT_FILE)
        ok("WebUI 读到了自定义目录里的心跳", int(hb.get("ts") or 0) > 0, str(hb)[:90])
        ok("phase=waiting_login 被保留（守护进程据此不清零重启计数）",
           hb.get("phase") == "waiting_login", hb.get("phase"))

        print("[5] 反向：配置文件缺失时退回默认目录而不是抛异常")
        srv._room_data_file("1999999999", HEARTBEAT_FILE)
        ok("无 config.yaml 的房间不抛异常", True)

        print("[6] 配置写坏（sqlite_path 为空）也不崩")
        bad = tmp / "rooms" / "1000000003"
        (bad / "data").mkdir(parents=True, exist_ok=True)
        (bad / "config.yaml").write_text("storage:\n  sqlite_path: \"\"\n", encoding="utf-8")
        p = srv._room_data_file("1000000003", HEARTBEAT_FILE)
        ok("空白 sqlite_path 退回 <room>/data", p == bad / "data" / HEARTBEAT_FILE, str(p))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED: {len(fails)} 项 -> {fails}")
        return 1
    print("ROOM STATE DIR OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
