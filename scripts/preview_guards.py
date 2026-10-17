"""预览：带几条上舰记录的隔离预览（用于截图核对 UI）。

用法: python scripts/preview_guards.py [端口]  → 账号 preview / preview123
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 19893


def main() -> None:
    import uvicorn

    import scripts.preview_ui as preview
    from app.config import load_config
    from app.storage import StatsStore
    from app.web import server as srv

    cfg_path = preview.prepare()
    db_path = preview.SCRATCH / "rooms" / preview.ROOM_ID / "data" / "bot.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = StatsStore(str(db_path))
    now = int(time.time())
    rows = [
        (now - 7200, 425396596, "ZeroTallyS", 3, "舰长", 1, 138000, 1380),
        (now - 3600, 343916903, "麻鼠易得", 3, "舰长", 3, 414000, 4140),
        (now - 1800, 8184943, "极光处刑", 2, "提督", 1, 1998000, 19980),
        (now - 600, 412983042, "玻璃", 3, "舰长", 1, 138000, 1380),
    ]
    for i, (ts, uid, uname, lvl, gname, months, coin, val) in enumerate(rows):
        store.record_guard_event(ts=ts, month="2026-10", uid=uid, uname=uname,
                                 guard_level=lvl, guard_name=gname, months=months,
                                 price_coin=coin, value=val, start_time=ts - 1,
                                 source="USER_TOAST_MSG_V2")
    store.close()

    srv.init_app(load_config(cfg_path), config_path=cfg_path)
    print(f"preview(guards): http://127.0.0.1:{PORT}/  账号 {preview.USER} / 密码 {preview.PASS}", flush=True)
    uvicorn.run(srv.app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
