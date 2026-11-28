"""「机器人开着但什么都没反应」必须在界面上看得出来。

心跳里一直有 9 个字段 WebUI 从来没读过（只写不读）：

    danmaku_connected / danmaku_task_alive / event_silent_seconds
    bg_restarts / queue_depth / live / last_gift_ts / last_danmaku_ts / credential_status

其中前四个是用户报「有时候机器人莫名其妙就不动了 / 不说话了」时唯一的现场：

- 收弹幕的 Task 已经死了 → 一件礼物都不会再统计、关键词一句都不回；
- 连接半开（TCP 还在、数据不来）→ 守护进程要等 180 秒才重连，这期间完全静默；
- 后台任务反复崩溃被自动重建 → 「偶尔漏几条弹幕 / 偶尔不回话」。

这三种情况下进程活着、心跳在写、页面显示「运行中」，用户只会说「莫名其妙关了」。

    ./.venv/Scripts/python.exe scripts/check_conn_visibility.py
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROOM = "1946287911"
PORT = 18846
TOKEN = "connvis-token"


def main() -> int:
    import uvicorn
    import yaml
    from app.web import server as srv

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="connvis_"))
    try:
        room = tmp / "rooms" / ROOM
        (room / "data").mkdir(parents=True)
        raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        raw["room_display_id"] = 1946287911
        (room / "config.yaml").write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._API_TOKEN = TOKEN
        srv._SESSIONS[TOKEN] = (time.time() + 900, "tester", "admin", [])
        srv._rate_limited = lambda ip: False  # type: ignore[assignment]
        # 让房间显示为 running（真实判定要真有 Bot 在跑）
        srv.room_status = lambda rid: "running"  # type: ignore[assignment]

        def hb(payload: dict) -> None:
            payload.setdefault("ts", int(time.time()))
            (room / "data" / "bot_status.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="error")
        server = uvicorn.Server(cfg)
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)
        else:
            print("FAILED: uvicorn 未启动")
            return 2

        def rooms() -> dict:
            r = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/rooms")
            r.add_header("Cookie", f"session={TOKEN}")
            with urllib.request.urlopen(r, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("rooms", []):
                if item.get("room_id") == ROOM:
                    return item
            return {}

        print("[1] 收弹幕的 Task 死了 → 必须提示")
        hb({"danmaku_task_alive": False, "danmaku_connected": True,
            "event_silent_seconds": 400, "gift_seen": 0, "gift_recorded": 0})
        r = rooms()
        ci = r.get("conn_issue") or {}
        ok("有 conn_issue", bool(ci), r.get("conn_issue"))
        ok("kind = task_dead", ci.get("kind") == "task_dead", ci.get("kind"))

        print("[2] 连接半开（180 秒没事件）→ 提示")
        hb({"danmaku_task_alive": True, "danmaku_connected": True,
            "event_silent_seconds": 200})
        r = rooms()
        ok("kind = silent", (r.get("conn_issue") or {}).get("kind") == "silent",
           r.get("conn_issue"))
        ok("带上了静默秒数", (r.get("conn_issue") or {}).get("silent") == 200,
           r.get("conn_issue"))

        print("[3] 正常连接（30 秒内有事件）→ 不提示")
        hb({"danmaku_task_alive": True, "danmaku_connected": True,
            "event_silent_seconds": 25})
        ok("没有 conn_issue", not rooms().get("conn_issue"), rooms().get("conn_issue"))

        print("[4] 刚启动还没收到过事件（-1）→ 不提示")
        hb({"danmaku_task_alive": True, "danmaku_connected": True,
            "event_silent_seconds": -1})
        ok("没有 conn_issue", not rooms().get("conn_issue"), rooms().get("conn_issue"))

        print("[5] 心跳已经很久没更新（机器人根本没在跑）→ 不能拿旧数据报警")
        hb({"ts": int(time.time()) - 600, "danmaku_task_alive": False,
            "event_silent_seconds": 900})
        ok("没有 conn_issue", not rooms().get("conn_issue"), rooms().get("conn_issue"))

        print("[6] 房间不是 running → 不提示")
        hb({"danmaku_task_alive": False, "event_silent_seconds": 500})
        srv.room_status = lambda rid: "stopped"  # type: ignore[assignment]
        ok("没有 conn_issue", not rooms().get("conn_issue"), rooms().get("conn_issue"))
        srv.room_status = lambda rid: "running"  # type: ignore[assignment]

        print("[7] 后台任务被重建过 → 要让用户知道")
        hb({"danmaku_task_alive": True, "event_silent_seconds": 20, "bg_restarts": 3})
        r = rooms()
        ok("bg_restarts = 3", r.get("bg_restarts") == 3, r.get("bg_restarts"))
        ok("没有 bg_dead 时也显示（用另一条横幅）",
           not r.get("bg_dead") and r.get("bg_restarts") == 3, r.get("bg_dead"))

        print("[8] 阈值必须和守护进程一致")
        bot_src = (ROOT / "app" / "bot.py").read_text(encoding="utf-8")
        m = re.search(r"_DANMAKU_SILENCE_TIMEOUT\s*=\s*([0-9.]+)", bot_src)
        bot_val = float(m.group(1)) if m else None
        srv_src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        m2 = re.search(r"_DANMAKU_SILENCE_HINT\s*=\s*([0-9.]+)", srv_src)
        hint_val = float(m2.group(1)) if m2 else None
        ok("两个数字相等（守护重连 vs 界面提示）", bot_val == hint_val,
           {"bot": bot_val, "webui": hint_val})

        print("[9] 「运行中但心跳停了」必须能看出来")
        # 心跳每 15 秒写一次；60 秒没更新就说明它已经不干活了，
        # 但 `room_status` 只看进程号 → 页面还是🟢运行中。
        hb({"danmaku_task_alive": True, "event_silent_seconds": 30})
        r = rooms()
        ok("心跳新鲜时不报 stale", not r.get("stale_heartbeat"), r.get("stale_heartbeat"))
        old_ts = int(time.time()) - 300
        (room / "data" / "bot_status.json").write_text(json.dumps({
            "ts": old_ts, "danmaku_task_alive": True, "event_silent_seconds": 30,
        }), encoding="utf-8")
        r = rooms()
        ok("stale_heartbeat 报出秒数", int(r.get("stale_heartbeat") or 0) >= 290,
           r.get("stale_heartbeat"))
        ok("status_note 说明心跳停了", "没有心跳" in (r.get("status_note") or ""),
           r.get("status_note"))
        ok("心跳停了就不再用旧数据报连接问题", not r.get("conn_issue"),
           r.get("conn_issue"))

        print("[10] status_note 以前算了但从来没渲染 → 现在必须在卡片上显示")
        srv_src2 = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        ok("房间卡片渲染 r.status_note", '{{ r.status_note }}' in srv_src2)
        ok("详情页有心跳停了横幅", "selectedRoom.stale_heartbeat" in srv_src2)
        ok("说明礼物不会统计/弹幕不会回复", "礼物不会统计、弹幕不会回复" in srv_src2)

        print("[11] UI 必须有对应横幅")
        ok("有 conn_issue 横幅", "selectedRoom.conn_issue" in srv_src)
        ok("有 bg_restarts 横幅", "selectedRoom.bg_restarts > 0" in srv_src)
        ok("说明「不会收到任何弹幕和礼物」", "不会收到任何弹幕和礼物" in srv_src)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("CONN VISIBILITY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
