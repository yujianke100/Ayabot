"""「精美导出」不能被几万条明细打死（也不能静静地少一半）。

这一页是给人看 / 截图用的多列排版，不是数据导出通道。热门房间一个月就有
几万条礼物，全量返回 + 全量渲染会让浏览器直接卡死（Chrome 会判「页面无响应」）。

现在的行为：
  - 后端最多返回 `_EXPORT_MAX_ROWS` 条，但**排序之后**才截断
    （否则「按价值排」拿到的是"随便 N 条里最贵的"，结果不对）；
  - 真实总数走响应头 `X-Ayabot-Total-Rows` / `X-Ayabot-Truncated`，
    响应体仍然是一个裸数组（旧前端不用改）；
  - 前端被截断时明确提示「共 N 条，只显示前部分，请缩小日期范围」，
    而且这句提示不会被后面的「无送礼记录」覆盖掉。

    ./.venv/Scripts/python.exe scripts/check_export_cap.py
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

PORT = 19025
ROOM = "1946287911"
TOKEN = "exportcap"
ROWS = 4500


def main() -> int:
    import uvicorn
    from app.storage import GiftEvent, StatsStore
    from app.web import server as srv

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="exportcap_"))
    try:
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (tmp / "data").mkdir(parents=True)
        (tmp / "config.yaml").write_text(example, encoding="utf-8")
        room = tmp / "rooms" / ROOM
        (room / "data").mkdir(parents=True)
        (room / "config.yaml").write_text(example, encoding="utf-8")

        print(f"造 {ROWS} 条礼物（价值递增，便于验证排序后截断）")
        st = StatsStore(str(room / "data" / "bot.db"))
        now = int(time.time())
        for i in range(ROWS):
            st.record_gift_event(GiftEvent(
                ts=now - (ROWS - i), month=time.strftime("%Y-%m", time.localtime(now)),
                uid=1000 + (i % 50), uname=f"u{i % 50}", event_type="SEND_GIFT",
                gift_name="辣条", gift_num=1, is_blind_box=0, blind_box_cost=0,
                actual_value=i,                      # 越大越贵
                profit_value=0,
                # batch_combo_id 唯一 → 不会被导出接口合并成同一行
                # （合并键是 (uid, batch_combo_id, gift_name)，全空的话 4500 条会塌成 50 行）
                raw_json=json.dumps({"total_coin": (i + 1) * 100,
                                     "batch_combo_id": f"b{i}"})), commit=False)
        st._conn.commit()
        st.close()

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._API_TOKEN = TOKEN
        srv._SESSIONS[TOKEN] = (time.time() + 3600, "t", "admin", [])
        srv._rate_limited = lambda ip: False  # type: ignore[assignment]

        cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="critical")
        server = uvicorn.Server(cfg)
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(150):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)

        def get(path: str):
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                         headers={"Cookie": f"session={TOKEN}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                # 不要 dict(r.headers)：HTTP 头名大小写不敏感（FastAPI 会输出小写），
                # 转成普通 dict 之后 `hdrs.get("X-...")` 就成了大小写敏感的，
                # 断言会因为「头名小写」而假红。r.headers 本身支持不区分大小写。
                return r.status, r.headers, r.read()

        print("[1] 默认请求被截断，但总数如实回报")
        code, hdrs, body = get(f"/api/rooms/{ROOM}/export_all?date_from=&date_to=&gift_type=all&sort=ts")
        rows = json.loads(body)
        ok("HTTP 200", code == 200, code)
        ok(f"返回条数被夹到上限（{srv._EXPORT_MAX_ROWS}）",
           len(rows) == srv._EXPORT_MAX_ROWS, len(rows))
        ok("总数头正确", hdrs.get("X-Ayabot-Total-Rows") == str(ROWS),
           hdrs.get("X-Ayabot-Total-Rows"))
        ok("截断标记为 1", hdrs.get("X-Ayabot-Truncated") == "1",
           hdrs.get("X-Ayabot-Truncated"))
        ok("响应体仍然是裸数组（旧前端不用改）", isinstance(rows, list))

        print("[2] 按价值排序：截断必须发生在排序之后")
        code, hdrs, body = get(
            f"/api/rooms/{ROOM}/export_all?date_from=&date_to=&gift_type=all&sort=value")
        rows = json.loads(body)
        vals = [r.get("actual_value", 0) for r in rows]
        ok("还是被夹到上限", len(rows) == srv._EXPORT_MAX_ROWS, len(rows))
        ok("**第一条是最贵的**（不是最早的那 N 条里挑）",
           vals and max(vals) == vals[0], f"首条={vals[0] if vals else None} 最大={max(vals) if vals else None}")
        ok("整段是递减的", all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)))
        ok("最便宜的那条不在结果里（说明真的只取了前 N 名）",
           min(vals) > 0, min(vals) if vals else None)

        print("[3] 没有数据的日期范围：不截断、如实报 0")
        # 注意不能用「今天」—— 上面造的 4500 条全落在今天（跨约 75 分钟），
        # 那样断言会一直截断（我第一版就是这么写错的）
        code, hdrs, body = get(
            f"/api/rooms/{ROOM}/export_all?date_from=2020-01-01&date_to=2020-01-02"
            f"&gift_type=all&sort=ts")
        ok("返回空数组", json.loads(body) == [], body[:40])
        ok("total=0", hdrs.get("X-Ayabot-Total-Rows") == "0",
           hdrs.get("X-Ayabot-Total-Rows"))
        ok("truncated=0", hdrs.get("X-Ayabot-Truncated") == "0",
           hdrs.get("X-Ayabot-Truncated"))

        print("[4] 前端会读这两个头并提示")
        src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        seg = src[src.index("async function loadExport"):]
        seg = seg[: seg.index("async function ", 20)]
        ok("读 X-Ayabot-Truncated", "X-Ayabot-Truncated" in seg)
        ok("读总数头", "X-Ayabot-Total-Rows" in seg)
        ok("提示里给出总数和「缩小日期范围」", "缩小日期范围" in seg)
        ok("前端用 res.headers.get（浏览器里不区分大小写）",
           "res.headers.get('X-Ayabot-Truncated')" in seg)
        ok("「无送礼记录」不会覆盖截断提示",
           "!exportList.value.length && !errExport.value" in seg)

        server.should_exit = True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("EXPORT CAP OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
