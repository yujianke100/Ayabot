"""上舰记录 WebUI 自检：/api/rooms/<id>/guards 与「上舰记录」面板。

全程在隔离预览目录（scratch/preview_root）里跑，不碰真实 rooms/ 数据。

用法: ./.venv/Scripts/python.exe scripts/check_guards_api.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 19894
USER, PASS = "preview", "preview123"
ROOM_ID = "1946287911"


def _req(path: str, cookie: str = "", method: str = "GET", body: dict | None = None):
    headers = {"Cookie": cookie} if cookie else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers


def main() -> int:
    import uvicorn

    import scripts.preview_ui as preview
    from app.config import load_config
    from app.storage import StatsStore
    from app.web import server as srv

    cfg_path = preview.prepare()

    # 往隔离房间的库里塞几条上舰记录
    db_path = Path(srv._ROOMS_BASE_DIR or preview.SCRATCH) if False else (preview.SCRATCH / "rooms" / ROOM_ID / "data" / "bot.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = StatsStore(str(db_path))
    now = int(time.time())
    store.record_guard_event(ts=now - 300, month="2026-10", uid=111, uname="舰长甲",
                             guard_level=3, guard_name="舰长", months=1,
                             price_coin=138000, value=1380, start_time=now - 400, source="GUARD_BUY")
    store.record_guard_event(ts=now - 200, month="2026-10", uid=222, uname="提督乙",
                             guard_level=2, guard_name="提督", months=2,
                             price_coin=1998000, value=19980, start_time=now - 300, source="USER_TOAST_MSG_V2")
    store.record_guard_event(ts=now - 100, month="2026-10", uid=333, uname="总督丙",
                             guard_level=1, guard_name="总督", months=1,
                             price_coin=19980000, value=199800, start_time=now - 200, source="GUARD_BUY")
    store.close()

    srv.init_app(load_config(cfg_path), config_path=cfg_path)
    server = uvicorn.Server(uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.5)
    if not server.started:
        print("FAILED: uvicorn 未启动")
        return 1

    failures: list[str] = []
    try:
        code, body, headers = _req("/api/login", method="POST",
                                   body={"username": USER, "password": PASS})
        if code != 200:
            print("login failed:", code, body[:200])
            return 1
        cookie = headers.get("Set-Cookie", "").split(";")[0]
        print("login cookie:", cookie.split("=")[0])

        code, body, _ = _req(f"/api/rooms/{ROOM_ID}/guards", cookie)
        print(f"/api/rooms/{ROOM_ID}/guards -> {code}")
        data = json.loads(body) if code == 200 else {}
        print("totals:", data.get("totals"))
        for it in (data.get("items") or [])[:5]:
            print("  ", it)
        if code != 200:
            failures.append(f"guards API -> {code}")
        items = data.get("items") or []
        if len(items) != 3:
            failures.append(f"上舰记录条数 {len(items)}（应为 3）")
        totals = data.get("totals") or {}
        if totals.get("count") != 3:
            failures.append(f"汇总 count 不对: {totals}")
        if totals.get("captain") != 1 or totals.get("commander") != 1 or totals.get("governor") != 1:
            failures.append(f"各等级计数不对: {totals}")
        if totals.get("value") != 1380 + 19980 + 199800:
            failures.append(f"价值合计不对: {totals}")
        # 最新的排最前
        if items and items[0].get("uname") != "总督丙":
            failures.append(f"排序不对（首条 {items[0].get('uname')}）")

        # 日期过滤
        code, body, _ = _req(f"/api/rooms/{ROOM_ID}/guards?rStart=2000-01-01&rEnd=2000-01-02", cookie)
        filtered = json.loads(body) if code == 200 else {}
        if filtered.get("items"):
            failures.append("日期过滤无效")

        # 页面里要有面板
        code, html, _ = _req("/", cookie)
        if code != 200:
            failures.append(f"/ -> {code}")
        for needle in ("上舰记录", "guardItems", "loadGuards", "/guards?"):
            if needle not in html:
                failures.append(f"页面缺少: {needle}")
        print("page bytes:", len(html))
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nGUARDS API OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
