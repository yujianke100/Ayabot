"""遗留接口（单房间时代留下的 4 个 + 老扫码保存）在**多房间部署**下的行为自检。

背景（两个都是"看起来成功、实际没生效"的静默故障）：

A. `/api/ranking` `/api/user_gifts` `/api/user_dates` `/api/delete_old`
   读的是根 config 的 `storage.sqlite_path`。多房间模式下那个库根本不存在，
   而 `sqlite3.connect()` **会顺手把它创建出来**（0 字节），接口再抛
   `no such table: gift_events` → 用户目录里多一个空库 + 前端拿到 500。
   要求：不许创建文件；只读接口退化到第一个有数据的房间库；破坏性接口报错。

B. `/api/bili_login/save` 只写 `<config目录>/data/credential.json`，
   而 `AuthManager` 优先读 `accounts/<uid>/credential.json`、
   降级读 `rooms/<id>/data/credential.json` —— 两边都对不上，
   于是"扫码成功、保存成功、机器人仍说凭证失效"，用户只能一遍遍重扫。

    ./.venv/Scripts/python.exe scripts/check_legacy_endpoints.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN = "legacy-token"
PORT = 18917


def main() -> int:
    import uvicorn
    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_legacy_"))
    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    try:
        # 多房间布局：根目录没有 data/bot.db，只有 rooms/<id>/data/bot.db
        (tmp / "data").mkdir(parents=True, exist_ok=True)
        room = tmp / "rooms" / "1946287911"
        (room / "data").mkdir(parents=True, exist_ok=True)
        (room / "config.yaml").write_text(
            (ROOT / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")

        from app.storage import GiftEvent, StatsStore
        st = StatsStore(str(room / "data" / "bot.db"))
        now = int(time.time())
        st.record_gift_event(GiftEvent(
            ts=now, month=StatsStore.month_of_ts(now), uid=42, uname="甲",
            event_type="SEND_GIFT", gift_name="辣条", gift_num=3, is_blind_box=False,
            blind_box_cost=0, actual_value=300, profit_value=0,
            raw_json=json.dumps({"total_coin": 300, "giftName": "辣条", "num": 3})))
        st.close()

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")   # 故意不存在
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._API_TOKEN = TOKEN
        srv._SESSIONS[TOKEN] = (time.time() + 900, "tester", "admin", [])
        srv._rate_limited = lambda ip: False          # type: ignore[assignment]

        global_db = Path(srv._DB_PATH)
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

        def req(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
            data = json.dumps(body).encode() if body is not None else None
            r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method)
            r.add_header("Cookie", f"session={TOKEN}")
            r.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(r, timeout=30) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8", "replace")

        today = time.strftime("%Y-%m-%d")
        print("[1] 只读遗留接口：不造文件，退化到房间库")
        code, body = req("GET", f"/api/ranking?start={today}&end={today}")
        ok("ranking 不再 500", code != 500, f"HTTP {code}")
        ok("ranking 从房间库读到了数据", code == 200 and '"uid": 42' in body.replace('"uid":42', '"uid": 42'),
           body[:100])
        ok("**没有凭空创建全局库**", not global_db.exists(),
           f"{global_db} ({global_db.stat().st_size} B)" if global_db.exists() else "未创建")

        code2, _ = req("GET", "/api/user_dates?uid=42")
        ok("user_dates 不再 500", code2 != 500, f"HTTP {code2}")
        ok("user_dates 仍不造文件", not global_db.exists())

        print("[2] 破坏性遗留接口：找不到全局库必须拒绝，不许猜房间")
        code3, body3 = req("POST", "/api/delete_old", {"date": today})
        ok("delete_old 拒绝执行", code3 == 404, f"HTTP {code3} {body3[:80]}")
        from app.storage import StatsStore as SS
        st2 = SS(str(room / "data" / "bot.db"))
        left = st2._conn.execute("SELECT COUNT(1) FROM gift_events").fetchone()[0]
        st2._conn.close()
        ok("房间数据一条都没被删", left == 1, f"剩 {left} 条")

        print("[3] 老扫码保存：必须写进 accounts/<uid>/credential.json")
        src = (ROOT / "app/web/server.py").read_text(encoding="utf-8")
        ok("保存逻辑包含 accounts 目录",
           '_get_account_dir(uid)' in src and "acc_dir / \"credential.json\"" in src)
        ok("保留旧路径兜底（单房间/老部署）",
           'legacy_path = Path(_CONFIG_YAML_PATH).parent / "data" / "credential.json"' in src)
        ok("uid 必须是纯数字才拼目录", "if not uid.isdigit()" in src)
        # 与 AuthManager 的读取优先级对齐（这是真正的不变量）
        from app.auth import AuthManager
        from app.config import load_config
        cfg_room = load_config(str(room / "config.yaml"))
        cfg_room.account_uid = "42"
        am = AuthManager(cfg_room)
        paths = [str(p) for p in am._credential_candidate_paths()]
        ok("AuthManager 候选路径包含 accounts/<uid>/credential.json",
           any("accounts/42/credential.json" in p.replace("\\", "/") for p in paths), paths)
        # account_uid 被赋成 int 时不能抛（Path / int 会 TypeError，而这条链路在凭证恢复上）
        cfg_room.account_uid = 42  # type: ignore[assignment]
        try:
            am2 = AuthManager(cfg_room)
            am2._credential_candidate_paths()
            crashed = False
        except TypeError:
            crashed = True
        ok("account_uid 是 int 也不崩", not crashed)

        print("[4] 全局库真的存在时，遗留接口照旧用它（不回退）")
        srv._DB_PATH = str(tmp / "data" / "solo.db")
        solo = Path(srv._DB_PATH)
        st3 = SS(str(solo))
        st3.record_gift_event(GiftEvent(
            ts=now, month=SS.month_of_ts(now), uid=7, uname="乙",
            event_type="SEND_GIFT", gift_name="小心心", gift_num=1, is_blind_box=False,
            blind_box_cost=0, actual_value=0, profit_value=0,
            raw_json=json.dumps({"total_coin": 0})))
        st3.close()
        code4, body4 = req("GET", f"/api/ranking?start={today}&end={today}")
        ok("单房间模式仍读根库", code4 == 200 and '"uid": 7' in body4.replace('"uid":7', '"uid": 7'),
           body4[:90])

        server.should_exit = True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("LEGACY ENDPOINTS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
