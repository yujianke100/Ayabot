"""校验「房间库路径」与「统计接口金额单位」。

覆盖：
  1. WebUI 读的库必须和机器人写的库是同一个
     （房间 config.yaml 里 storage.sqlite_path 不是默认 data/bot.db 时）
  2. 绝对路径 / 相对路径 / 缺配置 三种情况
  3. 改了 config.yaml 之后缓存要失效
  4. 外部 API 的金额单位是「元」：金瓜子 ÷1000、电池 ÷10
  5. 房间排行接口的 count 不能再是写死的 0

不动真实 rooms/：所有数据在临时目录里。

用法: ./.venv/Scripts/python.exe scripts/check_room_db_path.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 19897
TOKEN = "test-session-token"
API_TOKEN = "ext-api-token"
ROOM = "1977000111"
ROOM_CUSTOM = "1977000222"


def _get(path: str, cookie: str = TOKEN) -> tuple[int, str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", headers={"Cookie": f"session={cookie}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    import uvicorn

    from app.config import load_config
    from app.storage import GiftEvent, StatsStore
    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_dbpath_"))
    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert 'sqlite_path: "data/bot.db"' in example, "config.example.yaml 结构变了，需要更新本脚本"

    # 房间 A：默认路径；房间 B：自定义相对路径
    for rid, rel in ((ROOM, "data/bot.db"), (ROOM_CUSTOM, "data/my_stats.db")):
        d = tmp / "rooms" / rid
        (d / "data").mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(
            example.replace('sqlite_path: "data/bot.db"', f'sqlite_path: "{rel}"'),
            encoding="utf-8")

    srv._ROOMS_BASE_DIR = str(tmp)
    srv._ROOM_DB_PATH_CACHE.clear()
    srv._API_TOKEN = API_TOKEN
    srv._SESSIONS[TOKEN] = (time.time() + 900, "tester", "admin", [])
    srv._rate_limited = lambda ip: False  # type: ignore[assignment]
    srv._GIFT_CACHE_BUILT = True
    srv._GIFT_PRICE_CACHE = {"辣条": (100, "silver"), "幸运盲盒": (5000, "gold")}

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    try:
        # ── 1. 路径解析 ──
        print("[1] 房间库路径以 config.storage.sqlite_path 为准")
        p_default = srv._room_db_path(ROOM)
        p_custom = srv._room_db_path(ROOM_CUSTOM)
        ok("默认路径仍是 data/bot.db",
           p_default == tmp / "rooms" / ROOM / "data" / "bot.db", str(p_default))
        ok("自定义路径被采纳",
           p_custom == tmp / "rooms" / ROOM_CUSTOM / "data" / "my_stats.db", str(p_custom))

        # 与机器人侧的解析必须一致（这才是真正的不变量）
        for rid in (ROOM, ROOM_CUSTOM):
            bot_side = Path(load_config(str(tmp / "rooms" / rid / "config.yaml")).storage.sqlite_path)
            ok(f"[{rid}] WebUI 路径 == 机器人写入路径",
               srv._room_db_path(rid) == bot_side, f"{srv._room_db_path(rid)} vs {bot_side}")

        # ── 2. 绝对路径 ──
        print("[2] 绝对路径")
        abs_db = tmp / "elsewhere" / "abs.db"
        cfg_b = tmp / "rooms" / ROOM_CUSTOM / "config.yaml"
        cfg_b.write_text(
            cfg_b.read_text(encoding="utf-8").replace(
                'sqlite_path: "data/my_stats.db"', f'sqlite_path: "{abs_db.as_posix()}"'),
            encoding="utf-8")
        # mtime 分辨率可能相同，显式 sleep 一毫秒再改
        time.sleep(0.01)
        cfg_b.write_text(cfg_b.read_text(encoding="utf-8"), encoding="utf-8")
        ok("绝对路径被采纳", srv._room_db_path(ROOM_CUSTOM) == abs_db,
           str(srv._room_db_path(ROOM_CUSTOM)))
        ok("与机器人侧一致",
           srv._room_db_path(ROOM_CUSTOM) ==
           Path(load_config(str(cfg_b)).storage.sqlite_path))

        # 改回相对路径，验证缓存会失效
        cfg_b.write_text(
            cfg_b.read_text(encoding="utf-8").replace(
                f'sqlite_path: "{abs_db.as_posix()}"', 'sqlite_path: "data/my_stats.db"'),
            encoding="utf-8")
        ok("改回相对路径后缓存失效",
           srv._room_db_path(ROOM_CUSTOM) == tmp / "rooms" / ROOM_CUSTOM / "data" / "my_stats.db",
           str(srv._room_db_path(ROOM_CUSTOM)))

        # ── 3. 缺 config.yaml ──
        print("[3] 没有 config.yaml 时退回默认路径")
        (tmp / "rooms" / "no_cfg_room").mkdir(parents=True, exist_ok=True)
        ok("退到 data/bot.db",
           srv._room_db_path("no_cfg_room") == tmp / "rooms" / "no_cfg_room" / "data" / "bot.db")

        # ── 4. WebUI 真的能从自定义路径读到数据 ──
        print("[4] 数据写在自定义库里，接口要能查到")
        st = StatsStore(str(p_custom.parent / "my_stats.db"))
        # 一般礼物：total_coin=5000 金瓜子 = 5 元；盲盒：actual_value=52 电池 = 5.2 元
        st.record_gift_event(GiftEvent(
            ts=int(time.time()), month=time.strftime("%Y-%m"), uid=1001, uname="甲",
            event_type="SEND_GIFT", gift_name="辣条", gift_num=2, is_blind_box=0,
            blind_box_cost=0, actual_value=0, profit_value=0,
            raw_json=json.dumps({"total_coin": 5000, "num": 2})))
        st.record_gift_event(GiftEvent(
            ts=int(time.time()), month=time.strftime("%Y-%m"), uid=1001, uname="甲",
            event_type="SEND_GIFT", gift_name="星光铃铛", gift_num=1, is_blind_box=1,
            blind_box_cost=500, actual_value=52, profit_value=-448,
            raw_json=json.dumps({"blind_gift": {"original_gift_price": 5000,
                                                "gift_tip_price": 520}})))
        st._conn.close()
        # 确认默认路径下没有库（旧代码会去读它并创建一个空的）
        ok("默认 bot.db 没被误创建", not p_custom.parent.joinpath("bot.db").exists(),
           str(p_custom.parent / "bot.db"))

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

        code, body = _get(f"/api/rooms/{ROOM_CUSTOM}/ranking?rStart=&rEnd=&rType=all")
        ok("自定义库排行接口返回 200", code == 200, code)
        rows = (json.loads(body).get("ranking") or []) if code == 200 else []
        ok("排行榜能查到自定义库里的数据", len(rows) == 1, rows)
        if rows:
            row = rows[0]
            # 5.0（一般礼物）+ 5.2（盲盒）= 10.2 元
            ok("金额单位是元（10.2 而不是 102）", abs(float(row["total"]) - 10.2) < 0.01,
               row["total"])
            ok("利润是元（-44.8 而不是 -448）", abs(float(row["total_profit"]) + 44.8) < 0.01,
               row["total_profit"])
            ok("count 不再是写死的 0", int(row["count"]) == 2, row["count"])

        # ── 5. 外部 API 的金额单位 ──
        print("[5] 外部 API 金额单位")
        code, body = _get(f"/api/external/ranking?room_id={ROOM_CUSTOM}&period=all"
                          f"&token={API_TOKEN}")
        ok("外部接口返回 200", code == 200, f"{code} {body[:100]}")
        data = json.loads(body) if code == 200 else {}
        ok("外部接口 ok=true", data.get("ok") is True, data.get("error"))
        ext = (data.get("ranking") or [])
        ok("外部接口返回 1 个用户", len(ext) == 1, ext)
        if ext:
            e = ext[0]
            # 单位契约：外部接口统一用「电池」（金瓜子÷100；盲盒 actual_value 本身是电池）。
            # 一般礼物 5000 金瓜子 = 50 电池，盲盒 52 电池 → 102 电池 = 10.2 元
            ok("外部 value = 102 电池（与 user_stats 同单位）", int(e["value"]) == 102, e["value"])
            ok("外部 profit = -448 电池", int(e["profit"]) == -448, e["profit"])
            ok("外部 value_yuan = 10.2", abs(float(e["value_yuan"]) - 10.2) < 0.01, e["value_yuan"])
            ok("外部 count = 3 件（SUM(gift_num)，不是行数）", int(e["count"]) == 3, e["count"])

            # 两个外部接口的金额单位必须一致（曾经一个改成元、一个还是电池，差 10 倍）
            code2, body2 = _get(f"/api/external/user_stats?room_id={ROOM_CUSTOM}&uid=1001"
                                f"&period=all&token={API_TOKEN}")
            ok("user_stats 返回 200", code2 == 200, body2[:120])
            us = json.loads(body2) if code2 == 200 else {}
            us_val = int((us.get("gift") or {}).get("total_value", -1))
            ok("两个外部接口金额同单位（电池）", us_val == int(e["value"]),
               {"user_stats": us_val, "ranking": e["value"]})
            ok("user_stats 的盲盒利润同为电池",
               int((us.get("blindbox") or {}).get("profit", 0)) == int(e["profit"]),
               {"user_stats": (us.get("blindbox") or {}).get("profit"), "ranking": e["profit"]})

        # 默认库的房间也应该正常（不能被自定义路径的修复带坏）
        code, body = _get(f"/api/rooms/{ROOM}/ranking?rStart=&rEnd=&rType=all")
        ok("默认路径房间接口正常", code == 200, code)
    finally:
        try:
            server.should_exit = True
        except UnboundLocalError:
            pass
        try:
            th.join(timeout=10)
        except UnboundLocalError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(" -", f)
        return 1
    print("\nROOM DB PATH OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
