"""「统计全是 0，但界面一条提示都没有」和「凭证失效被误判成已恢复」的回归。

两个 bug 都是**看用户所见**才能发现的，所以这里直接打 `/api/rooms`：

A. 礼物缺 uid 时不会有任何提示
   `_handle_gift_payload` 在 uid<=0 时**先 return**，`gift_seen` 不加一；
   而 `/api/rooms` 以前的判定是 `seen > 0 and recorded == 0`。
   于是「匿名连接（凭证失效）→ 所有礼物被丢弃」这件事：
     - `dropped_no_uid` 一直在涨
     - `gift_seen` 永远是 0
     - `gift_issue` 永远不置 → 界面上「缺用户信息通常是登录态失效」那一支是死代码
   用户看到的就是「统计全是 0，但页面一切正常」。

B. 凭证其实还没恢复，红条却被收起来了
   `/api/rooms` 用「credential.json 比 auth_status.json 新 5 秒以上」来猜
   「用户已经重新扫码」。但 `credential.json` 会被**任何**共用同一账号的房间在
   验证成功时重写（`AuthManager._save_credential`），mtime 就新了。
   心跳里 `waiting_login=true` 才是「我还在等扫码」的事实 —— 必须以此为准。

    ./.venv/Scripts/python.exe scripts/check_gift_issue_visibility.py
"""
from __future__ import annotations

import json
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
UID = 4242
PORT = 18837
TOKEN = "giftissue-token"


def main() -> int:
    import uvicorn
    from app.storage import GiftEvent, StatsStore
    from app.web import server as srv

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="giftissue_"))
    try:
        room = tmp / "rooms" / ROOM
        (room / "data").mkdir(parents=True)
        (tmp / "accounts" / str(UID)).mkdir(parents=True)
        # 房间必须绑定账号，否则 `/api/rooms` 里那条「credential 比 status 新」的
        # 启发式根本不会执行（它需要 account_uid 才能定位 credential.json）
        import yaml
        raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        raw["account_uid"] = UID
        raw["room_display_id"] = 1946287911
        (room / "config.yaml").write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._API_TOKEN = TOKEN
        srv._SESSIONS[TOKEN] = (time.time() + 900, "tester", "admin", [])
        srv._rate_limited = lambda ip: False  # type: ignore[assignment]

        def write_json(path: Path, payload: dict) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

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

        print("[1] 礼物全部因缺少 uid 被丢弃 → 界面必须提示")
        write_json(room / "data" / "bot_status.json", {
            "ts": int(time.time()),
            # 关键：一件都没进入解析，所以 gift_seen 是 0
            "gift_seen": 0,
            "gift_recorded": 0,
            "dropped_no_uid": 7,
            # 30 秒前还在丢 → 这是「现在就有问题」
            "no_uid_age_seconds": 30,
            "danmaku_connected": True,
            "danmaku_task_alive": True,
        })
        r = rooms()
        issue = r.get("gift_issue") or {}
        ok("有 gift_issue（以前这里什么都没有）", bool(issue), r.get("gift_issue"))
        ok("no_uid 传到了前端", issue.get("no_uid") == 7, issue.get("no_uid"))
        ok("seen 如实是 0（不编造）", issue.get("seen") == 0, issue.get("seen"))

        print("[2] 累计值不清零、但「最近没再丢」就不再提示")
        # 机器人侧：成功记上一件礼物**不能**清空 _dropped_no_uid
        # （清了「已累计 N 条」就不真实，半好半坏时红条还会一闪一闪）
        from app.bot import LiveRobot
        from app.config import load_config
        cfg_file = room / "config.yaml"
        loaded = load_config(str(cfg_file))
        store = StatsStore(str(tmp / "data" / "bot.db"))

        class _FakeAuth:
            status = "ok"

            def write_status(self, *a, **k) -> None:
                pass

            def mark_needs_login(self, *a, **k) -> None:
                pass

        robot = LiveRobot(config=loaded, credential=object(), auth=_FakeAuth())
        robot._enqueue_reply = lambda *a, **k: None  # type: ignore[assignment]
        # 走真实路径（不要直接戳属性，否则测的是「我赋值成功没」）：
        # 4 件缺少 uid 的礼物
        for _ in range(4):
            robot._warn_gift_without_uid("", "辣条")

        import asyncio
        asyncio.run(robot._handle_gift_payload({
            "uid": 999, "uname": "甲", "giftName": "辣条", "num": 1,
            "tid": "giftissue-1", "timestamp": int(time.time()), "total_coin": 100,
        }, "SEND_GIFT"))
        ok("记上一条之后累计值**保留**（诊断数据不篡改）", robot._dropped_no_uid == 4,
           robot._dropped_no_uid)
        ok("最后一次丢弃时刻仍记着", getattr(robot, "_last_no_uid_ts", 0) > 0,
           getattr(robot, "_last_no_uid_ts", 0))

        # 心跳：「1 小时前丢过」——累计值还在，但不算当前故障
        write_json(room / "data" / "bot_status.json", {
            "ts": int(time.time()), "gift_seen": 1, "gift_recorded": 1,
            "dropped_no_uid": 4, "no_uid_age_seconds": 3600,
            "danmaku_connected": True, "danmaku_task_alive": True,
        })
        r2 = rooms()
        ok("一小时没再丢 → 提示消失（红条不会永久挂着）", not r2.get("gift_issue"),
           r2.get("gift_issue"))

        # 边界：刚恢复（5 分钟内还在丢）就必须还在
        write_json(room / "data" / "bot_status.json", {
            "ts": int(time.time()), "gift_seen": 1, "gift_recorded": 1,
            "dropped_no_uid": 4, "no_uid_age_seconds": 200,
            "danmaku_connected": True, "danmaku_task_alive": True,
        })
        r2b = rooms()
        ok("200 秒前刚丢过 → 仍然提示", bool(r2b.get("gift_issue")), r2b.get("gift_issue"))

        print("[3] 凭证失效时红条必须一直在（不能被 credential mtime 骗掉）")
        write_json(room / "data" / "auth_status.json", {
            "status": "needs_login", "reason": "Cookie 过期", "ts": int(time.time()) - 600,
        })
        # 让 credential.json 明显比 auth_status.json 新（模拟「另一个房间验证成功后重写了它」）
        cred = tmp / "accounts" / str(UID) / "credential.json"
        cred.write_text(json.dumps({"SESSDATA": "x"}), encoding="utf-8")
        old = time.time() - 600
        import os
        os.utime(room / "data" / "auth_status.json", (old, old))

        write_json(room / "data" / "bot_status.json", {
            "ts": int(time.time()), "waiting_login": True,
            "phase": "waiting_login", "danmaku_connected": False,
        })
        r3 = rooms()
        ok("心跳在等登录 → 仍然是 needs_login", r3.get("auth_status") == "needs_login",
           r3.get("auth_status"))
        ok("原因文案还在", bool(r3.get("auth_reason")), r3.get("auth_reason"))
        # status_note 只在房间真的是 running 时才写（进程存活与否由 process_manager 判），
        # 这个测试里没有真机器人，不能断言它 —— 那会变成「因为错误的原因而通过」。

        print("[4] 机器人真的恢复后（心跳不再等登录），红条才该收起")
        write_json(room / "data" / "bot_status.json", {
            "ts": int(time.time()), "waiting_login": False,
            "phase": "running", "danmaku_connected": True,
        })
        r4 = rooms()
        ok("此时才认为已恢复", r4.get("auth_status") == "ok", r4.get("auth_status"))

        print("[5] 写库失败（礼物真的丢了）必须在界面上说出来")
        # `_record_stat` 失败时只加 `_dropped_stats` 并写一行日志；
        # 以前 WebUI 从不读这个字段 —— 用户看到的是「统计比实际少」，页面一切正常。
        write_json(room / "data" / "bot_status.json", {
            "ts": int(time.time()), "gift_seen": 5, "gift_recorded": 2,
            "dropped_no_uid": 0, "no_uid_age_seconds": -1,
            "dropped_stats": 3, "stats_fail_age_seconds": 12,
            "danmaku_connected": True, "danmaku_task_alive": True,
        })
        r5 = rooms()
        si = r5.get("stats_issue") or {}
        ok("有 stats_issue", bool(si), r5.get("stats_issue"))
        ok("报出失败次数", si.get("dropped") == 3, si.get("dropped"))
        ok("报出最近一次多久之前", si.get("age") == 12, si.get("age"))
        ok("同时不再显示 gift_issue（没有缺 uid 的礼物）", not r5.get("gift_issue"),
           r5.get("gift_issue"))

        write_json(room / "data" / "bot_status.json", {
            "ts": int(time.time()), "gift_seen": 5, "gift_recorded": 5,
            "dropped_no_uid": 0, "no_uid_age_seconds": -1,
            "dropped_stats": 3, "stats_fail_age_seconds": 7200,
            "danmaku_connected": True, "danmaku_task_alive": True,
        })
        r6 = rooms()
        ok("两小时没再失败 → 不再提示（累计值不清零）", not r6.get("stats_issue"),
           r6.get("stats_issue"))

        # UI 得有对应的横幅，否则前端什么都不显示
        srv_src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        ok("UI 有 stats_issue 横幅", "selectedRoom.stats_issue" in srv_src)
        ok("横幅说明「不会补记」", "不会补记" in srv_src)

        store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("GIFT ISSUE VISIBILITY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
