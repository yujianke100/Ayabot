"""校验「重数据接口不能卡住事件循环」。

为什么重要：exe（冻结）模式下 WebUI 和**所有房间的机器人**共用同一个 asyncio 事件循环
（`process_manager._use_inprocess()`）。任何在 async 端点里同步跑的 SQLite 全表查询，
都会同时冻结每个房间的发消息、弹幕处理、心跳。

实测成本（20 万行）：
  - `/api/rooms/{id}/export_all`：取数 0.28s + 逐行 json.loads 0.26s ≈ 0.54s
  - `/api/rooms/{id}/all_dates`：逐行 Python 回调用 ayabot_date(ts) ≈ 0.35s
100 万行时分别约 2.7s / 1.8s。用户的感觉就是「点了一下导出，机器人就哑了」。

做法：起 uvicorn，同时发一个重请求和一个轻请求，量轻请求被拖了多久。
  - 修复后（to_thread）：轻请求延迟基本不受影响
  - 若把 to_thread 换成同步调用：轻请求会被拖到和重请求一样久

用法: ./.venv/Scripts/python.exe scripts/check_eventloop_block.py
"""
from __future__ import annotations

import asyncio
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

PORT = 19896
TOKEN = "test-session-token"
ROOM = "1988000111"
ROWS = 150_000


def _get(path: str) -> tuple[int, str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", headers={"Cookie": f"session={TOKEN}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def main() -> int:
    import uvicorn

    from app.config import load_config
    from app.storage import GiftEvent, StatsStore
    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_evloop_"))
    room_dir = tmp / "rooms" / ROOM
    (room_dir / "data").mkdir(parents=True, exist_ok=True)
    (room_dir / "config.yaml").write_text(
        (ROOT / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    srv._ROOMS_BASE_DIR = str(tmp)
    srv._ROOM_DB_PATH_CACHE.clear()
    srv._SESSIONS[TOKEN] = (time.time() + 900, "tester", "admin", [])
    srv._rate_limited = lambda ip: False  # type: ignore[assignment]

    # 造一份有真实体量的库
    db = room_dir / "data" / "bot.db"
    st = StatsStore(str(db))
    st._conn.execute("BEGIN")
    raw = json.dumps({"total_coin": 100,
                      "sender_uinfo": {"base": {"face": "https://x/y.jpg"}},
                      "gift_info": {"img_basic": "https://i/z.png"},
                      "batch_combo_id": "b"})
    t0 = time.time()
    for i in range(ROWS):
        st.record_gift_event(GiftEvent(
            ts=1735664400 + i * 30, month="2024-12", uid=1000 + i % 500,
            uname=f"u{i % 500}", event_type="SEND_GIFT", gift_name="辣条", gift_num=1,
            is_blind_box=0, blind_box_cost=0, actual_value=10, profit_value=0,
            raw_json=raw), dedupe_key=f"k{i}", source="xml:x#a", commit=False)
    st._conn.commit()
    st.close()
    print(f"造库 {ROWS} 行，耗时 {time.time() - t0:.1f}s")

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    server = uvicorn.Server(uvicorn.Config(srv.app, host="127.0.0.1", port=PORT,
                                           log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(80):
        if server.started:
            break
        time.sleep(0.25)
    if not server.started:
        print("FAILED: uvicorn 未启动")
        return 1

    def measure(heavy_path: str, label: str) -> tuple[float, float]:
        """同时发重请求 + 轻请求，返回 (重请求耗时, 轻请求耗时)。"""
        heavy_s = [0.0]
        light_s = [0.0]

        def heavy():
            t = time.time()
            try:
                _get(heavy_path)
            finally:
                heavy_s[0] = time.time() - t

        ht = threading.Thread(target=heavy)
        ht.start()
        time.sleep(0.08)          # 让重请求先进入服务端
        t = time.time()
        _get("/api/time")
        light_s[0] = time.time() - t
        ht.join(timeout=180)
        print(f"  {label}: 重 {heavy_s[0]*1000:.0f}ms | 轻 {light_s[0]*1000:.0f}ms")
        return heavy_s[0], light_s[0]

    try:
        # 预热（首次请求有建连接等开销）
        _get("/api/time")
        _get(f"/api/rooms/{ROOM}/all_dates")

        # ── 1. 修复后的行为 ──
        print("[1] to_thread 版本：重请求不阻塞轻请求")
        _, light_export = measure(f"/api/rooms/{ROOM}/export_all?date_from=&date_to=",
                                 "export_all")
        ok(f"导出期间 /api/time 仍在 {light_export*1000:.0f}ms 内返回（< 300ms）",
           light_export < 0.3, f"{light_export*1000:.0f}ms")

        _, light_dates = measure(f"/api/rooms/{ROOM}/all_dates", "all_dates")
        ok(f"取日期期间 /api/time 仍在 {light_dates*1000:.0f}ms 内返回（< 300ms）",
           light_dates < 0.3, f"{light_dates*1000:.0f}ms")

        # ── 2. 反向验证：改回同步调用，必须复现阻塞 ──
        print("[2] 反向验证：同步调用必然阻塞（证明上面那条断言有意义）")
        real_to_thread = srv.asyncio.to_thread

        async def _inline(fn, *a, **kw):   # 直接在事件循环里跑
            return fn(*a, **kw)

        srv.asyncio.to_thread = _inline  # type: ignore[assignment]
        try:
            _, light_blocked = measure(
                f"/api/rooms/{ROOM}/export_all?date_from=&date_to=", "export_all(同步)")
        finally:
            srv.asyncio.to_thread = real_to_thread  # type: ignore[assignment]
        ok(f"同步调用时轻请求被明显拖慢（≥ {light_blocked*1000:.0f}ms，应 > 200ms）",
           light_blocked > 0.2, f"{light_blocked*1000:.0f}ms")
        print(f"  → 对比：{light_export*1000:.0f}ms（线程） vs "
              f"{light_blocked*1000:.0f}ms（同步）")

        # ── 3. 结构断言：这两个端点必须走 to_thread ──
        print("[3] 结构断言")
        src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        seg = src[src.index("async def api_room_export_all"):]
        seg = seg[:seg.index("\ndef ", 10)]
        ok("export_all 使用 asyncio.to_thread", "asyncio.to_thread" in seg)
        seg2 = src[src.index("async def api_room_all_dates"):]
        seg2 = seg2[:seg2.index("\ndef ", 10)]
        ok("all_dates 使用 asyncio.to_thread", "asyncio.to_thread" in seg2)
    finally:
        server.should_exit = True
        th.join(timeout=15)
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(" -", f)
        return 1
    print("\nEVENTLOOP BLOCK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
