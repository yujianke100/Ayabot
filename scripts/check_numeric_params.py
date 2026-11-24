"""所有数值型查询参数都要夹取：-1 / 0 / 10^9 都不能 500，也不能拖全表。

这是第 27 轮修过的同一类坑的推广：`get_danmaku_log(limit=-1)` 在 SQLite 里等于
`LIMIT -1` = **不限**（实测返回全表）。当时只修了那一个接口；这里把每个
数值参数都用极端值打一遍，逐个断言：

  - 不能 5xx
  - 返回条数不能超过接口自己的上限
  - 负数/0 要按「没指定」处理，不能变成 unlimited

    ./.venv/Scripts/python.exe scripts/check_numeric_params.py
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

PORT = 18973
ROOM = "1946287911"
TOKEN = "num-params"

#: (标签, 路径模板, 响应里「条数」怎么算, 上限)
CASES = [
    ("弹幕分页", "/api/danmaku_log?room_id={room}&limit={v}",
     lambda b: len(b.get("items") or b.get("rows") or []), 1000),
    ("弹幕分页 offset", "/api/danmaku_log?room_id={room}&limit=10&offset={v}",
     lambda b: len(b.get("items") or b.get("rows") or []), 10),
    ("房间日志行数", "/api/rooms/{room}/log?lines={v}",
     lambda b: len((b.get("lines") or []) if isinstance(b, dict) else (b or [])), 5000),
    ("上舰列表", "/api/rooms/{room}/guards?rStart=&rEnd=&limit={v}",
     lambda b: len((b.get("items") or [])), 500),
    ("排行", "/api/rooms/{room}/ranking?rStart=&rEnd=&rType=all",
     lambda b: len((b.get("ranking") or [])), 50),
    ("用户礼物流水", "/api/rooms/{room}/user_gifts?uid=27950910&date=2026-06-17",
     lambda b: len(b) if isinstance(b, list) else len(b.get("items") or []), 100000),
]

VALUES = [-1, 0, 1, 10 ** 9, -10 ** 9]


def main() -> int:
    import uvicorn
    from app.web import server as srv
    from app.storage import StatsStore
    from app.danmaku_xml import parse_danmaku_xml

    tmp = Path(tempfile.mkdtemp(prefix="numparam_"))
    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    try:
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (tmp / "data").mkdir(parents=True)
        (tmp / "config.yaml").write_text(example, encoding="utf-8")
        room = tmp / "rooms" / ROOM
        (room / "data").mkdir(parents=True)
        (room / "config.yaml").write_text(example, encoding="utf-8")

        # 造 1500 条弹幕 + 100 条礼物，才能区分「夹取」和「全表」
        db = room / "data" / "bot.db"
        st = StatsStore(str(db))
        base = 1770000000
        for i in range(1500):
            st.record_danmaku(ts=base + i, uid=1000 + i % 5, uname=f"u{i}", content=f"c{i}",
                              max_entries=0)
        from app.storage import GiftEvent
        for i in range(100):
            st.record_gift_event(GiftEvent(
                ts=base + i, month=StatsStore.month_of_ts(base + i), uid=27950910,
                uname="甲", event_type="SEND_GIFT", gift_name="辣条", gift_num=1,
                is_blind_box=False, blind_box_cost=0, actual_value=100, profit_value=0,
                raw_json='{"total_coin":100}'))
        st._conn.close()

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._API_TOKEN = TOKEN
        srv._SESSIONS[TOKEN] = (time.time() + 3600, "t", "admin", [])
        srv._rate_limited = lambda ip: False  # type: ignore[assignment]
        srv._GIFT_CACHE_BUILT = True
        srv._GIFT_PRICE_CACHE = {"辣条": (100, "silver")}

        cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="critical")
        server = uvicorn.Server(cfg)
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(120):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)

        for tag, tmpl, counter, cap in CASES:
            print(f"[{tag}]")
            for v in VALUES:
                path = tmpl.format(room=ROOM, v=v)
                req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                             headers={"Cookie": f"session={TOKEN}"})
                try:
                    with urllib.request.urlopen(req, timeout=90) as r:
                        code, raw = r.status, r.read().decode("utf-8", "replace")
                except urllib.error.HTTPError as exc:
                    code, raw = exc.code, exc.read().decode("utf-8", "replace")
                try:
                    body = json.loads(raw)
                except Exception:
                    body = {"_raw": raw[:100]}
                try:
                    n = counter(body)
                except Exception:
                    n = -1
                bad = ""
                if code >= 500 or code == 0:
                    bad = "  <<< 5xx"
                    fails.append(f"{tag} v={v}: {code}")
                elif n > cap:
                    bad = f"  <<< 返回 {n} 条 > 上限 {cap}"
                    fails.append(f"{tag} v={v}: {n} 条 > {cap}")
                print(f"  {code:3d}  v={v:<12} 条数={n}{bad}")

        server.should_exit = True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails[:8]}")
        return 1
    print("NUMERIC PARAMS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
