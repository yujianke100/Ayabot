"""校验「历史数据导入（录播弹幕 XML）」的解析、换算、去重与撤销。

覆盖：
  1. 有 raw 的 XML   → 金额用真实 total_coin，盲盒能完整还原（成本/实际/盈亏）
  2. 无 raw 的 XML   → 按礼物名查表估算金额；盲盒不做统计；未知礼物名单独报数
  3. 无 <metadata>   → 用 <d p> 的发送时间戳反推录制时间
  4. 无任何时间信息  → 必须报错，不许瞎猜日期
  5. 重复导入        → dedupe_key 命中，不重复计数，月度聚合不涨
  6. 与实时数据重叠  → 能被识别（预检报数），skip_overlap 打开后真的跳过
  7. 撤销导入        → 行删除 + 月度聚合精确扣回
  8. 权限            → 普通用户 403
不动真实 rooms/：所有数据在临时目录里。

用法: ./.venv/Scripts/python.exe scripts/check_xml_import.py
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

PORT = 19893
TOKEN = "test-session-token"
ROOM = "1999888777"

# 录制起始时间：2024-12-01 18:05:02 +08:00
BASE_ISO = "2024-12-01T18:05:02+08:00"
BASE_TS = 1733047502  # 与上面一致；下面用 datetime 校验

XML_NO_RAW = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <chatserver>chat.bilibili.com</chatserver>
  <chatid>0</chatid>
  <metadata>
    <user_name>测试主播</user_name>
    <room_id>1999888777</room_id>
    <live_start_time>2024-12-01T18:03:59+08:00</live_start_time>
    <record_start_time>2024-12-01T18:05:02+08:00</record_start_time>
  </metadata>
  <d p="0.000,1,25,16777215,1733047505000,0,111,0" user="甲">第一句话</d>
  <d p="12.500,1,25,16777215,1733047518000,0,222,0" user="乙">第二句</d>
  <gift ts="11.000" uid="111" user="甲" giftname="辣条" giftcount="2"/>
  <gift ts="13.000" uid="222" user="乙" giftname="小花花" giftcount="1"/>
  <gift ts="14.000" uid="0" user="游客" giftname="辣条" giftcount="1"/>
  <gift ts="15.000" uid="333" user="丙" giftname="某个新礼物" giftcount="3"/>
  <guard ts="18.000" uid="444" user="丁" level="3" count="2"/>
  <sc ts="50.000" uid="555" user="戊" price="30" time="60">醒目留言内容</sc>
</i>
"""

XML_WITH_RAW = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <BililiveRecorderRecordInfo roomid="1999888777" name="测试主播" start_time="{start}"/>
  <gift ts="10.000" uid="111" user="甲" giftname="星光铃铛" giftcount="1"
        raw='{{"giftName":"星光铃铛","num":1,"price":5200,"total_coin":5000,"coin_type":"gold","blind_gift":{{"original_gift_price":5000,"gift_tip_price":5200}}}}'/>
  <gift ts="20.000" uid="222" user="乙" giftname="告白气球" giftcount="1"
        raw='{{"giftName":"告白气球","num":1,"price":200000,"total_coin":200000,"coin_type":"gold","blind_gift":null}}'/>
  <guard ts="30.000" uid="444" user="丁" level="2" count="1"
         raw='{{"price":1998000,"num":1,"start_time":1733047530000}}'/>
</i>
"""

XML_NO_META = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <d p="5.000,1,25,16777215,1733047507000,0,111,0" user="甲">只有弹幕</d>
  <gift ts="10.000" uid="111" user="甲" giftname="小花花" giftcount="1"/>
</i>
"""

XML_NO_TIME = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <gift ts="10.000" uid="111" user="甲" giftname="小花花" giftcount="1"/>
</i>
"""

XML_ONLY_CHAT = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <metadata><record_start_time>{start}</record_start_time></metadata>
  <d p="1.000,1,25,16777215,1733047503000,0,111,0" user="甲">只是一句弹幕</d>
</i>
""".format(start=BASE_ISO)


def _req(method: str, path: str, body: bytes | None = None,
         headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=body, method=method,
        headers={"Cookie": f"session={TOKEN}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw[:200]}


def main() -> int:
    import uvicorn

    from app.config import load_config
    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_xmlimport_"))
    room_dir = tmp / "rooms" / ROOM
    (room_dir / "data").mkdir(parents=True, exist_ok=True)
    yaml_src = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    (room_dir / "config.yaml").write_text(yaml_src, encoding="utf-8")

    srv._ROOMS_BASE_DIR = str(tmp)
    srv._SESSIONS[TOKEN] = (time.time() + 900, "tester", "admin", [])
    srv._SESSIONS["user-token"] = (time.time() + 900, "normal", "user", [])
    # 固定价格表，避免用例依赖网络
    srv._GIFT_PRICE_CACHE = {"辣条": (100, "silver"), "小花花": (100, "gold")}
    srv._GIFT_CACHE_BUILT = True

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

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    def post(path: str, xml: str, name: str, **params):
        q = "&".join(f"{k}={v}" for k, v in params.items())
        full = f"{path}?{q}" if q else path
        return _req("POST", full, xml.encode("utf-8"), {"X-Filename": name,
                                                        "Content-Type": "text/xml"})

    import sqlite3
    db = room_dir / "data" / "bot.db"

    def q(sql: str, args=()):
        conn = sqlite3.connect(str(db))
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    try:
        # ── 1. 无 raw：预检 ──
        print("[1] 无 raw 的 XML（预检）")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_NO_RAW, "no_raw.xml",
                       dry_run=1)
        ok("预检返回 200", code == 200, r.get("error"))
        st = r.get("stats") or {}
        ok("礼物 3 条（uid=0 的那条被丢弃）", st.get("gifts") == 3, st.get("gifts"))
        ok("上舰 1 条", st.get("guards") == 1, st.get("guards"))
        ok("弹幕计数 2 条（不导入）", st.get("chat") == 2, st.get("chat"))
        ok("SC 计数 1 条（不导入）", st.get("sc") == 1, st.get("sc"))
        ok("丢弃无 UID 1 条", st.get("dropped_no_uid") == 1, st.get("dropped_no_uid"))
        ok("金额按礼物名估算 = 辣条2×0.1 + 小花花0.1 = 0.3",
           abs(float(st.get("value_yuan") or 0) - 0.3) < 1e-6, st.get("value_yuan"))
        ok("上舰金额 舰长2月 = 396 元",
           abs(float(st.get("guard_value_yuan") or 0) - 396.0) < 1e-6,
           st.get("guard_value_yuan"))
        ok("未知价格礼物被单独统计", st.get("unknown_price_events") == 1,
           st.get("unknown_price_names"))
        ok("无 raw 时给出盲盒警告",
           any("盲盒" in w for w in (st.get("warnings") or [])), st.get("warnings"))
        ok("预检不落库", q("SELECT COUNT(1) FROM gift_events")[0][0] == 0)

        # ── 2. 无 raw：真导入 ──
        print("[2] 无 raw 的 XML（真导入）")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_NO_RAW, "no_raw.xml")
        st = r.get("stats") or {}
        ok("导入返回 200", code == 200 and r.get("ok"), r.get("error"))
        ok("写入 3 条", st.get("inserted") == 3, st.get("inserted"))
        ok("上舰写入 1 条", st.get("guards_inserted") == 1, st.get("guards_inserted"))
        rows = q("SELECT uid, gift_name, gift_num, is_blind_box, source FROM gift_events ORDER BY id")
        ok("库里 3 行", len(rows) == 3, rows)
        ok("source 标记为 xml:", all(str(x[4]).startswith("xml:") for x in rows), rows[0][4] if rows else None)
        ok("无 raw 不会误判成盲盒", all(x[3] == 0 for x in rows), [x[3] for x in rows])
        g = q("SELECT guard_level, months, price_coin, value FROM guard_events")
        ok("上舰 舰长×2 月 396000 金瓜子", g and g[0][:3] == (3, 2, 396000), g)

        # ── 3. 重复导入同一文件 ──
        print("[3] 重复导入同一份 XML")
        before = q("SELECT COUNT(1), COALESCE(SUM(gift_num),0) FROM gift_events")[0]
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_NO_RAW, "no_raw.xml")
        st = r.get("stats") or {}
        after = q("SELECT COUNT(1), COALESCE(SUM(gift_num),0) FROM gift_events")[0]
        ok("新增 0 条", st.get("inserted") == 0, st.get("inserted"))
        ok("全部命中去重", st.get("skipped_duplicate") == 3, st.get("skipped_duplicate"))
        ok("行数与数量不变", before == after, (before, after))
        agg_rows = q("SELECT SUM(gift_count), SUM(gift_num_total) FROM monthly_gift_stats")[0]
        ok("月度聚合不涨（3 次 / 6 件）", tuple(agg_rows) == (3, 6), agg_rows)

        # ── 4. 有 raw：盲盒完整还原 ──
        print("[4] 带 raw 的 XML（盲盒可还原）")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml",
                       XML_WITH_RAW.format(start=BASE_ISO), "with_raw.xml", dry_run=1)
        st = r.get("stats") or {}
        ok("预检成功", code == 200 and r.get("ok"), r.get("error"))
        ok("识别出 1 条盲盒", st.get("blindbox") == 1, st.get("blindbox"))
        # 52 电池 = 5.2 元（10 电池 = 1 元）；告白气球 total_coin 200000 = 200 元
        ok("金额 = 盲盒实际 5.2 元 + 告白气球 200 元 = 205.2",
           abs(float(st.get("value_yuan") or 0) - 205.2) < 1e-6, st.get("value_yuan"))

        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml",
                       XML_WITH_RAW.format(start=BASE_ISO), "with_raw.xml")
        st = r.get("stats") or {}
        ok("写入 2 条礼物", st.get("inserted") == 2, st.get("inserted"))
        b = q("SELECT blind_box_cost, actual_value, profit_value FROM gift_events WHERE is_blind_box=1")
        ok("盲盒：成本 50 / 实际 52 / 盈亏 2", b and tuple(b[0]) == (50, 52, 2), b)
        ok("上舰 提督 1998 元",
           q("SELECT price_coin FROM guard_events WHERE guard_level=2")[0][0] == 1998000)

        # ── 5. 无 metadata，靠弹幕时间戳反推 ──
        print("[5] 没有 <metadata> 的 XML")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_NO_META, "no_meta.xml",
                       dry_run=1)
        st = r.get("stats") or {}
        ok("预检成功", code == 200 and r.get("ok"), r.get("error"))
        ok("时间来源标为反推", st.get("base_source") == "弹幕时间戳反推", st.get("base_source"))
        ok("时间落在 2024-12-01", time.strftime("%Y-%m-%d", time.localtime(st.get("start_ts") or 0))
           == "2024-12-01", st.get("start_ts"))

        # ── 6. 缺时间信息 / 只有弹幕：必须明确报错 ──
        print("[6] 无法导入的文件要讲清楚原因")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_NO_TIME, "no_time.xml")
        ok("缺时间被拒", r.get("ok") is False and "起始时间" in (r.get("error") or ""),
           r.get("error"))
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_ONLY_CHAT, "chat.xml")
        ok("只有弹幕被拒并说明原因",
           r.get("ok") is False and "没有可用的事件" in (r.get("error") or ""), r.get("error"))
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", "<i><broken>", "bad.xml")
        ok("坏 XML 被拒", r.get("ok") is False and "XML" in (r.get("error") or ""), r.get("error"))

        # ── 7. 与实时数据重叠 ──
        print("[7] 和实时记录重叠")
        # 先撤掉前面那份导入，腾出位置只留「实时」那条，避免被 dedupe_key 直接吃掉
        _req("DELETE", f"/api/rooms/{ROOM}/imports?source=xml:no_raw.xml")
        conn = sqlite3.connect(str(db))
        ts0 = BASE_TS + 11
        conn.execute(
            "INSERT INTO gift_events (ts, month, uid, uname, event_type, gift_name, gift_num,"
            " is_blind_box, blind_box_cost, actual_value, profit_value, raw_json, dedupe_key, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts0, time.strftime("%Y-%m", time.localtime(ts0)), 111, "甲", "SEND_GIFT", "辣条", 2,
             0, 0, 0, 0, '{"total_coin":200}', "", ""))
        conn.commit()
        conn.close()
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_NO_RAW, "no_raw.xml",
                       dry_run=1)
        st = r.get("stats") or {}
        ok("预检报出重叠 1 条", st.get("overlap_gifts") == 1, st.get("overlap_gifts"))
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_NO_RAW, "no_raw.xml",
                       skip_overlap=1)
        st = r.get("stats") or {}
        ok("skip_overlap 跳过 1 条", st.get("skipped_overlap") == 1, st.get("skipped_overlap"))
        ok("其余 2 条正常写入", st.get("inserted") == 2, st.get("inserted"))
        dup = q("SELECT COUNT(1) FROM gift_events WHERE uid=111 AND gift_name='辣条'")
        ok("辣条没有重复成 2 行（实时 1 + 导入 0）", dup[0][0] == 1, dup)

        # ── 8. 批次列表 + 撤销 ──
        print("[8] 导入批次与撤销")
        code, r = _req("GET", f"/api/rooms/{ROOM}/imports")
        imports = r.get("imports") or []
        ok("列出 2 个批次", len(imports) == 2, [i["name"] for i in imports])
        target = next((i for i in imports if i["name"] == "with_raw.xml"), None)
        ok("批次含礼物条数与上舰数", bool(target) and target["gift_events"] == 2
           and target["guard_events"] == 1, target)
        agg_before = q("SELECT COUNT(1), COALESCE(SUM(gift_num),0), COALESCE(SUM(actual_value),0)"
                       " FROM gift_events")[0]
        code, r = _req("DELETE", f"/api/rooms/{ROOM}/imports?source=xml:with_raw.xml")
        ok("撤销成功", code == 200 and r.get("ok"), r.get("error"))
        ok("删掉 2 条礼物 + 1 条上舰", r.get("deleted") == 2 and r.get("guard_deleted") == 1, r)
        ok("行真的没了", q("SELECT COUNT(1) FROM gift_events WHERE source='xml:with_raw.xml'")[0][0] == 0)
        ok("月度盲盒聚合已清零", q("SELECT COUNT(1) FROM monthly_blindbox_stats WHERE blind_box_count>0")[0][0] == 0)
        # 注意：第 7 步里「实时」那条是用 SQL 直接塞的，不经过月度聚合，所以只比导入行
        agg_after = q("SELECT COUNT(1), COALESCE(SUM(gift_num),0), COALESCE(SUM(actual_value),0)"
                      " FROM gift_events WHERE source LIKE 'xml:%'")[0]
        monthly = q("SELECT COALESCE(SUM(gift_count),0), COALESCE(SUM(gift_num_total),0),"
                    " COALESCE(SUM(value_total),0) FROM monthly_gift_stats")[0]
        ok("月度聚合与导入行一致", tuple(monthly) == tuple(agg_after), (monthly, agg_after))

        # ── 9. 权限 ──
        print("[9] 普通用户不能导入")
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/rooms/{ROOM}/import/danmaku-xml",
            data=XML_NO_RAW.encode(), method="POST",
            headers={"Cookie": "session=user-token", "X-Filename": "x.xml"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        ok("普通用户得到 403", code == 403, code)
    finally:
        server.should_exit = True
        th.join(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(" -", f)
        return 1
    print("\nXML IMPORT OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
