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
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

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


# 礼物名里带「盲盒」但没有 raw：绝不能算成盲盒（_extract_blindbox_profit 有名字兜底规则）
XML_BOX_NAME_NO_RAW = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <metadata><record_start_time>{start}</record_start_time></metadata>
  <gift ts="10.000" uid="111" user="甲" giftname="幸运盲盒" giftcount="1"/>
</i>
""".format(start=BASE_ISO)

# SEND_GIFT_V2 的 raw：只有 dmscore / pb（protobuf），没有金额字段 → 必须回退到价格表
XML_V2_RAW = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <metadata><record_start_time>{start}</record_start_time></metadata>
  <gift ts="10.000" uid="111" user="甲" giftname="小花花" giftcount="2"
        raw='{{"dmscore":62,"pb":"Cg=="}}'/>
</i>
""".format(start=BASE_ISO)


# SEND_GIFT_V2 真实报文（星光铃铛 / 盲盒 / 实付 5200 金瓜子、盒价 5000）：
# 录播姬开了「保存原始数据」时，<gift> 的 raw 就是 {"dmscore":..,"pb":"<这段>"}
V2_PB = (
    "CNfCjj0SDOatoumxvOS4jumypBpKaHR0cHM6Ly9pMS5oZHNsYi5jb20vYmZzL2ZhY2UvNzJjNmEwNDFhOTVjZDczODNkMmEwYzQyNTZmZGIwNGE2NDAyMmE5"
    "OC5qcGdCKgjmn7gMKB8yCeeMq+eMq+azpTjVkLQBQNWQtAFI/7f2BFD/0Z8DWAFgA0ogCJABEIaTAhoM5bm46L+Q55uy55uSKgbniIblh7owiCdS9wQIiJMC"
    "EgzmmJ/lhYnpk4Ppk5sYASACKNAoMNAoOIgnQgRnb2xkShM0ODI4Nzk3OTIxMjU0MjA1NDQwULKzzNYGWAFiJDZhOGIwMDlmLTcxYWQtNGM2Yi04NzdkLTc3"
    "NWFhMDQ2MmNmY2gKcNAoeAWFAQAAgD+IAQGSAQbmipXlloLAAaifpwrqARUKDeWNl+eUuk5pZ2h0aW4Qyfvg+QSKAuEBCMn74PkEEtgBCg3ljZfnlLpOaWdo"
    "dGluEkpodHRwczovL2kxLmhkc2xiLmNvbS9iZnMvZmFjZS9kNTJiMmUzNTNiMDg2OThhYzY2YmM2ZTQ1YmU0NGQwYWIwMGVhOGNlLmpwZzJbCg3ljZfnlLpO"
    "aWdodGluEkpodHRwczovL2kxLmhkc2xiLmNvbS9iZnMvZmFjZS9kNTJiMmUzNTNiMDg2OThhYzY2YmM2ZTQ1YmU0NGQwYWIwMGVhOGNlLmpwZzoeCAcSGmJp"
    "bGliaWxp55u05pKt6auY6IO95Li75pKtkgIAmgLlAQpKaHR0cHM6Ly9zMS5oZHNsYi5jb20vYmZzL2xpdmUvYTJlODI1NGY1YzUzM2E5YjNmMDAyMDFhODg3"
    "OGMzN2ZmZmQ0MDY1MS5wbmcSS2h0dHBzOi8vaTAuaGRzbGIuY29tL2Jmcy9saXZlL2ZjNWVlOWNiYjAzNWVmOTRlNjQyZDNjNjc2Zjc0MWZjMzFlNThkMjcu"
    "d2VicCpKaHR0cHM6Ly9pMC5oZHNsYi5jb20vYmZzL2xpdmUvYWU2YTkxMDZmZDY3MzdiNjg4YmI5M2EyOTM0NmUyNDA1ZjA1ZDcxOC5naWagAtAoqgIAWAFq"
    "AgggerECCNfCjj0SwwEKDOatoumxvOS4jumypBJKaHR0cHM6Ly9pMS5oZHNsYi5jb20vYmZzL2ZhY2UvNzJjNmEwNDFhOTVjZDczODNkMmEwYzQyNTZmZGIw"
    "NGE2NDAyMmE5OC5qcGcyWgoM5q2i6bG85LiO6bKkEkpodHRwczovL2kxLmhkc2xiLmNvbS9iZnMvZmFjZS83MmM2YTA0MWE5NWNkNzM4M2QyYTBjNDI1NmZk"
    "YjA0YTY0MDIyYTk4LmpwZzoLIP///////////wEaZAoJ5Y2h5ZW15oGwEAMYjq3yAiCOrfICKI6t8gIwjq3yAkgBUMn74PkEYA16CSM1NzYyQTc5OYIBCSM1"
    "NzYyQTc5OYoBCSM1NzYyQTc5OZIBByNGRkZGRkaaAQkjNTc2MkE3RTY="
)

XML_V2_BLIND = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <metadata><record_start_time>{start}</record_start_time></metadata>
  <gift ts="10.000" uid="128164183" user="止鱼与鲤" giftname="星光铃铛" giftcount="1"
        raw='{{"dmscore":62,"pb":"{pb}"}}'/>
</i>
""".format(start=BASE_ISO, pb=V2_PB)


# 同名不同内容的两份文件：批次必须能分开
XML_SAME_A = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <metadata><record_start_time>{start}</record_start_time></metadata>
  <gift ts="10.000" uid="111" user="甲" giftname="小花花" giftcount="1"/>
</i>
""".format(start=BASE_ISO)

XML_SAME_B = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <metadata><record_start_time>{start}</record_start_time></metadata>
  <gift ts="20.000" uid="222" user="乙" giftname="小花花" giftcount="3"/>
</i>
""".format(start=BASE_ISO)

# 单价 0 的免费礼物：不该进「未识别价格」名单
XML_FREE_GIFT = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <metadata><record_start_time>{start}</record_start_time></metadata>
  <gift ts="10.000" uid="111" user="甲" giftname="小心心" giftcount="5"/>
</i>
""".format(start=BASE_ISO)


# 跨月边界：UTC 是 2024-12-31 17:00，+08 已经是 2025-01-01 01:00。
# 用它验证「月份分桶用的是房间配置的时区，而不是系统本地时区」。
XML_TZ_EDGE = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <metadata><record_start_time>2024-12-31T17:00:00+00:00</record_start_time></metadata>
  <gift ts="10.000" uid="111" user="甲" giftname="小花花" giftcount="1"/>
</i>
"""

# 同一秒、同一个人、同一个礼物、同样数量出现两次：这是两笔真实交易，不能合并
XML_SAME_SECOND = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <metadata><record_start_time>{start}</record_start_time></metadata>
  <gift ts="10.000" uid="111" user="甲" giftname="小花花" giftcount="1"/>
  <gift ts="10.000" uid="111" user="甲" giftname="小花花" giftcount="1"/>
</i>
""".format(start=BASE_ISO)


# 跨两天各一条，用来验证「按日期删除」的边界与聚合重算
XML_TWO_DAYS = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <metadata><record_start_time>2024-12-01T20:00:00+08:00</record_start_time></metadata>
  <gift ts="0.000" uid="111" user="甲" giftname="小花花" giftcount="2"/>
  <gift ts="18000.000" uid="222" user="乙" giftname="小花花" giftcount="3"/>
</i>
"""


# 只含上舰记录、没有礼物的 XML（合法：录制期间没人送礼物，只上了舰）
XML_GUARD_ONLY = """<?xml version="1.0" encoding="utf-8"?>
<i>
  <metadata><record_start_time>{start}</record_start_time></metadata>
  <guard ts="100.000" uid="777" user="舰长甲" level="3" count="1"/>
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
    srv._GIFT_PRICE_CACHE = {"辣条": (100, "silver"), "小花花": (100, "gold"),
                             "小心心": (0, "silver")}
    srv._GIFT_CACHE_BUILT = True
    # 本脚本一口气打上百个请求，会被「每 IP 60 次/分钟」的限流挡住（429）。
    # 这是正式环境需要的保护，测试里关掉。
    srv._rate_limited = lambda ip: False  # type: ignore[assignment]

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

    db = room_dir / "data" / "bot.db"


    def q(sql: str, args=()):
        from app import tzutil
        conn = sqlite3.connect(str(db))
        try:
            # 测试里的裸连接也要注册时区函数，否则 SQL 里用不了 ayabot_date()
            tzutil.install_sqlite_functions(conn)
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def undo(name: str) -> dict:
        """按文件名撤销对应批次（source 里带内容摘要，得先查出来再 URL 编码）。"""
        rows = q("SELECT DISTINCT source FROM gift_events WHERE source LIKE ? "
                 "UNION SELECT DISTINCT source FROM guard_events WHERE source LIKE ?",
                 (f"xml:{name}#%", f"xml:{name}#%"))
        if not rows:
            return {"ok": False, "error": "批次不存在"}
        return _req("DELETE", f"/api/rooms/{ROOM}/imports?source={quote(rows[0][0])}")[1]

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
        ok("source 标记为 xml: 且带内容摘要",
           all(str(x[4]).startswith("xml:") and "#" in str(x[4]) for x in rows),
           rows[0][4] if rows else None)
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
        undo('no_raw.xml')
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
        code, r = _req("DELETE",
                       f"/api/rooms/{ROOM}/imports?source={quote(target['source'])}")
        ok("撤销成功", code == 200 and r.get("ok"), r.get("error"))
        ok("删掉 2 条礼物 + 1 条上舰", r.get("deleted") == 2 and r.get("guard_deleted") == 1, r)
        ok("行真的没了", q("SELECT COUNT(1) FROM gift_events WHERE source LIKE 'xml:with_raw.xml#%'")[0][0] == 0)
        ok("月度盲盒聚合已清零", q("SELECT COUNT(1) FROM monthly_blindbox_stats WHERE blind_box_count>0")[0][0] == 0)
        # 注意：第 7 步里「实时」那条是用 SQL 直接塞的，不经过月度聚合，所以只比导入行
        agg_after = q("SELECT COUNT(1), COALESCE(SUM(gift_num),0), COALESCE(SUM(actual_value),0)"
                      " FROM gift_events WHERE source LIKE 'xml:%'")[0]
        monthly = q("SELECT COALESCE(SUM(gift_count),0), COALESCE(SUM(gift_num_total),0),"
                    " COALESCE(SUM(value_total),0) FROM monthly_gift_stats")[0]
        ok("月度聚合与导入行一致", tuple(monthly) == tuple(agg_after), (monthly, agg_after))


        # ── 9. 名字含「盲盒」但没有 raw：不许算成盲盒 ──
        print("[9] 礼物名含「盲盒」但无 raw")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_BOX_NAME_NO_RAW,
                       "box_name.xml", dry_run=1)
        st = r.get("stats") or {}
        ok("能正常预检", code == 200 and r.get("ok"), r.get("error"))
        ok("不误判成盲盒", st.get("blindbox") == 0, st.get("blindbox"))
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_BOX_NAME_NO_RAW, "box_name.xml")
        row = q("SELECT is_blind_box, blind_box_cost, actual_value FROM gift_events"
                " WHERE gift_name='幸运盲盒'")
        ok("落库后 is_blind_box=0、成本 0", row and tuple(row[0]) == (0, 0, 0), row)
        undo('box_name.xml')

        # ── 10. V2 风格的 raw（只有 pb，没有金额）→ 回退价格表 ──
        print("[10] raw 里没有金额字段时回退价格表")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_V2_RAW, "v2raw.xml", dry_run=1)
        st = r.get("stats") or {}
        ok("能正常预检", code == 200 and r.get("ok"), r.get("error"))
        ok("没有把礼物算成「未知价格」", st.get("unknown_price_events") == 0,
           st.get("unknown_price_names"))
        ok("金额按价格表估 = 小花花 0.1×2 = 0.2 元",
           abs(float(st.get("value_yuan") or 0) - 0.2) < 1e-6, st.get("value_yuan"))
        ok("不算盲盒", st.get("blindbox") == 0, st.get("blindbox"))
        undo('v2raw.xml')

        # ── 11. 重复导入时上舰计数要分开报 ──
        print("[11] 重复导入的上舰计数")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_WITH_RAW.format(start=BASE_ISO),
                       "guard_dup.xml")
        ok("首次导入上舰 1 条", (r.get("stats") or {}).get("guards_inserted") == 1,
           (r.get("stats") or {}).get("guards_inserted"))
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_WITH_RAW.format(start=BASE_ISO),
                       "guard_dup.xml")
        st = r.get("stats") or {}
        ok("再次导入不重复计入新增", st.get("guards_inserted") == 0, st.get("guards_inserted"))
        ok("报成「已存在」1 条", st.get("guards_duplicate") == 1, st.get("guards_duplicate"))
        undo('guard_dup.xml')


        # ── 11bis. V2 真实报文 raw：能还原盲盒与精确金额 ──
        print("[11b] SEND_GIFT_V2 真实 raw")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_V2_BLIND, "v2blind.xml", dry_run=1)
        st = r.get("stats") or {}
        ok("能正常预检", code == 200 and r.get("ok"), r.get("error"))
        ok("识别为盲盒 1 条", st.get("blindbox") == 1, st.get("blindbox"))
        ok("金额按实付 52 电池 = 5.2 元",
           abs(float(st.get("value_yuan") or 0) - 5.2) < 1e-6, st.get("value_yuan"))
        ok("没有落入「未知价格」", st.get("unknown_price_events") == 0,
           st.get("unknown_price_names"))
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_V2_BLIND, "v2blind.xml")
        row = q("SELECT is_blind_box, blind_box_cost, actual_value, profit_value FROM gift_events"
                " WHERE source LIKE 'xml:v2blind.xml#%'")
        ok("落库：盲盒 成本50 实际52 盈亏2", row and tuple(row[0]) == (1, 50, 52, 2), row)
        undo('v2blind.xml')

        # ── 12b. NOT NULL 违规不能被静默吞掉 ──
        print("[12] 真错误要抛出来")
        from app.storage import GiftEvent as _GE, StatsStore as _SS
        _st = _SS(str(db))
        try:
            try:
                _st.record_gift_event(_GE(ts=0, month="2024-12", uid=1, uname=None,
                                          event_type="SEND_GIFT", gift_name="x", gift_num=1,
                                          is_blind_box=0, blind_box_cost=0, actual_value=0,
                                          profit_value=0, raw_json="{}"), dedupe_key="notnull1")
                raised = False
            except Exception:
                raised = True
            ok("uname 为 None 时抛异常（没被 OR IGNORE 吞掉）", raised)
        finally:
            _st._conn.close()


        # ── 14. 同名不同内容：两个独立批次，撤销一个不影响另一个 ──
        print("[14] 同名文件的批次隔离")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_SAME_A, "same.xml")
        ok("第一份导入成功", code == 200 and r.get("ok"), r.get("error"))
        src_a = (r.get("stats") or {}).get("source")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_SAME_B, "same.xml")
        ok("第二份也导入成功（不被当成同一批）", code == 200 and r.get("ok"), r.get("error"))
        src_b = (r.get("stats") or {}).get("source")
        ok("两份 source 不同", src_a and src_b and src_a != src_b, [src_a, src_b])
        imports = (_req("GET", f"/api/rooms/{ROOM}/imports")[1] or {}).get("imports") or []
        same = [i for i in imports if i["name"].startswith("same.xml")]
        ok("列表里出现两个同名批次", len(same) == 2, [i["name"] for i in same])
        ok("同名批次名字带摘要区分", all("(" in i["name"] for i in same),
           [i["name"] for i in same])
        code, r = _req("DELETE", f"/api/rooms/{ROOM}/imports?source={quote(src_a)}")
        ok("撤销第一份", code == 200 and r.get("deleted") == 1, r)
        left = q("SELECT COUNT(1) FROM gift_events WHERE source = ?", (src_b,))[0][0]
        ok("第二份的数据还在", left == 1, left)
        _req("DELETE", f"/api/rooms/{ROOM}/imports?source={quote(src_b)}")

        # ── 15. 单价 0 的免费礼物不算「未知价格」 ──
        print("[15] 免费礼物不误报未知价格")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_FREE_GIFT, "free.xml", dry_run=1)
        st = r.get("stats") or {}
        ok("预检成功", code == 200 and r.get("ok"), r.get("error"))
        ok("没有未知价格礼物", st.get("unknown_price_events") == 0,
           st.get("unknown_price_names"))
        ok("金额记 0", abs(float(st.get("value_yuan") or 0)) < 1e-6, st.get("value_yuan"))
        ok("数量照常计入", st.get("gift_num") == 5, st.get("gift_num"))


        # ── 17. 月份分桶必须跟房间配置的时区 ──
        print("[17] 月份分桶的时区口径")
        import yaml as _yaml
        cfg_file = room_dir / "config.yaml"
        original = cfg_file.read_text(encoding="utf-8")
        try:
            _raw = _yaml.safe_load(original) or {}
            _raw.setdefault("runtime", {})["timezone"] = "UTC"
            cfg_file.write_text(_yaml.safe_dump(_raw, allow_unicode=True, sort_keys=False),
                                encoding="utf-8")
            code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_TZ_EDGE, "tz_edge.xml")
            ok("导入成功", code == 200 and r.get("ok"), r.get("error"))
            month = q("SELECT DISTINCT month FROM gift_events WHERE source LIKE 'xml:tz_edge.xml#%'")
            ok("按配置的 UTC 落在 2024-12", month and month[0][0] == "2024-12", month)
            undo("tz_edge.xml")

            _raw.setdefault("runtime", {})["timezone"] = "Asia/Shanghai"
            cfg_file.write_text(_yaml.safe_dump(_raw, allow_unicode=True, sort_keys=False),
                                encoding="utf-8")
            code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_TZ_EDGE, "tz_edge2.xml")
            month = q("SELECT DISTINCT month FROM gift_events WHERE source LIKE 'xml:tz_edge2.xml#%'")
            ok("按配置的 Asia/Shanghai 落在 2025-01", month and month[0][0] == "2025-01", month)
            undo("tz_edge2.xml")
        finally:
            cfg_file.write_text(original, encoding="utf-8")

        # ── 18. 同一秒的重复礼物是两笔，不能被去重吃掉 ──
        print("[18] 同一秒的相同礼物")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_SAME_SECOND, "same_sec.xml")
        st = r.get("stats") or {}
        ok("两条都写入", st.get("inserted") == 2, st.get("inserted"))
        rows = q("SELECT COUNT(1) FROM gift_events WHERE source LIKE 'xml:same_sec.xml#%'")
        ok("库里 2 行", rows[0][0] == 2, rows)
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_SAME_SECOND, "same_sec.xml")
        st = r.get("stats") or {}
        ok("重复导入仍然是 0 新增", st.get("inserted") == 0, st.get("inserted"))
        ok("两条都判为重复", st.get("skipped_duplicate") == 2, st.get("skipped_duplicate"))
        undo("same_sec.xml")

        # ── 19. 并发导入同一份文件：不重复、不锁死 ──
        print("[19] 并发导入同一份文件")
        import threading as _th
        results: list = []
        lock = _th.Lock()

        def _worker():
            code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_SAME_A, "race.xml")
            with lock:
                results.append((code, (r.get("stats") or {}).get("inserted"),
                                r.get("error")))

        threads = [_th.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=90)
        codes = [c for c, _, _ in results]
        inserted = sum((i or 0) for _, i, _ in results)
        ok("4 个请求都成功", codes == [200, 200, 200, 200], results)
        ok("总共只新增 1 条（其余命中幂等）", inserted == 1, results)
        rows = q("SELECT COUNT(1) FROM gift_events WHERE source LIKE 'xml:race.xml#%'")
        ok("库里最终只有 1 条", rows[0][0] == 1, rows)
        # 清理：same.xml 的那份（第 14 组已撤，这里可能又建了一批）
        q_src = q("SELECT DISTINCT source FROM gift_events WHERE source LIKE 'xml:race.xml#%'")
        for (src,) in q_src:
            _req("DELETE", f"/api/rooms/{ROOM}/imports?source={quote(src)}")


        # ── 21. 按日期删除：边界与月度聚合 ──
        print("[21] 按日期删除（时区边界 + 聚合重算）")
        undo("two_days.xml")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_TWO_DAYS, "two_days.xml")
        ok("导入两天各一条", code == 200 and (r.get("stats") or {}).get("inserted") == 2,
           (r.get("stats") or {}).get("inserted"))
        days = [x[0] for x in q("SELECT DISTINCT ayabot_date(ts) FROM gift_events"
                               " WHERE source LIKE 'xml:two_days.xml#%' ORDER BY 1")]
        ok("落在两天（配置时区）", days == ["2024-12-01", "2024-12-02"], days)
        # 删掉 2024-12-01（含当天）→ 只剩 12-02 那条
        should_del = q("SELECT COUNT(1) FROM gift_events WHERE ayabot_date(ts) <= '2024-12-01'")[0][0]
        keep_cnt = q("SELECT COUNT(1) FROM gift_events WHERE ayabot_date(ts) > '2024-12-01'")[0][0]
        code, r = _req("POST", f"/api/rooms/{ROOM}/delete_old",
                       json.dumps({"date": "2024-12-01", "type": "gift"}).encode(),
                       {"Content-Type": "application/json"})
        ok("删除接口成功", code == 200, r)
        ok("删除条数与当天及更早的行数一致", r.get("deleted_gifts") == should_del,
           {"deleted": r.get("deleted_gifts"), "expected": should_del})
        ok("次日及以后的数据没被误删",
           q("SELECT COUNT(1) FROM gift_events")[0][0] == keep_cnt,
           {"left": q("SELECT COUNT(1) FROM gift_events")[0][0], "expected": keep_cnt})
        left = q("SELECT gift_num FROM gift_events WHERE source LIKE 'xml:two_days.xml#%'")
        ok("剩下的正是次日那条", [x[0] for x in left] == [3], left)
        agg = q("SELECT COALESCE(SUM(gift_count),0), COALESCE(SUM(gift_num_total),0),"
                " COALESCE(SUM(value_total),0) FROM monthly_gift_stats"
                " WHERE month='2024-12'")
        det = q("SELECT COUNT(1), COALESCE(SUM(gift_num),0), COALESCE(SUM(actual_value),0)"
                " FROM gift_events WHERE month='2024-12'")
        ok("月度聚合与明细一致（删完就重算）", tuple(agg[0]) == tuple(det[0]), [agg, det])
        undo("two_days.xml")

        # ── 23. 日期过滤改造后的各个接口都要还能用（ts 区间替换 ayabot_date）──
        print("[23] 日期过滤各接口")
        undo("api_dates.xml")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_SAME_A, "api_dates.xml")
        ok("准备一份当天数据", code == 200 and r.get("ok"), r.get("error"))
        day = q("SELECT ayabot_date(ts) FROM gift_events WHERE source LIKE 'xml:api_dates.xml#%'")
        day = day[0][0] if day else ""
        for label, path, key in (
            ("排行-当天", f"/api/rooms/{ROOM}/ranking?rStart={day}&rEnd={day}&rType=all", "ranking"),
            ("排行-不限", f"/api/rooms/{ROOM}/ranking?rStart=&rEnd=&rType=all", "ranking"),
            ("上舰-当天", f"/api/rooms/{ROOM}/guards?rStart={day}&rEnd={day}", "items"),
            ("上舰-不限", f"/api/rooms/{ROOM}/guards", "items"),
            ("导出日期列表", f"/api/rooms/{ROOM}/all_dates", None),
        ):
            code, r = _req("GET", path)
            ok(f"{label} 返回 200", code == 200, {"code": code, "body": str(r)[:90]})
            if key and isinstance(r, dict) and key in r:
                ok(f"{label} 含 {key}", True, len(r.get(key) or []))
        code, r = _req("GET", f"/api/rooms/{ROOM}/export_all?date_from={day}&date_to={day}")
        ok("导出明细返回 200", code == 200, {"code": code, "body": str(r)[:90]})
        code, r = _req("GET", f"/api/rooms/{ROOM}/ranking?rStart=bad-date&rEnd=&rType=all")
        ok("非法日期返回 400（不是 500）", code == 400, {"code": code, "body": str(r)[:90]})
        code, r = _req("GET", f"/api/rooms/{ROOM}/ranking?rStart={day}&rEnd={day}&rType=all")
        ok("当天筛选命中导入的礼物",
           any(str(x.get("uid")) == "111" for x in (r.get("ranking") or [])),
           (r.get("ranking") or [])[:2])
        undo("api_dates.xml")

        # ── 24. 空房间（从没跑过 bot）也能导入 ──
        print("[24] 空房间导入")
        empty_room = "2000000009"
        empty_dir = tmp / "rooms" / empty_room
        (empty_dir / "data").mkdir(parents=True, exist_ok=True)
        (empty_dir / "config.yaml").write_text(yaml_src, encoding="utf-8")
        code, r = post(f"/api/rooms/{empty_room}/import/danmaku-xml", XML_SAME_A, "empty.xml")
        ok("空房间导入成功", code == 200 and r.get("ok"), r.get("error"))
        ok("空房间建表并写入", (r.get("stats") or {}).get("inserted") == 1,
           (r.get("stats") or {}).get("inserted"))

        # ── 26. 只含上舰的批次：要能列出来、也要能撤销 ──
        print("[26] 仅含上舰的批次")
        undo("guard_only.xml")
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_GUARD_ONLY, "guard_only.xml")
        st = r.get("stats") or {}
        ok("导入成功且写入 1 条上舰",
           code == 200 and r.get("ok") and st.get("guards_inserted") == 1,
           st.get("guards_inserted"))
        ok("礼物数为 0", st.get("gifts") == 0, st.get("gifts"))
        imports = (_req("GET", f"/api/rooms/{ROOM}/imports")[1] or {}).get("imports") or []
        mine = [i for i in imports if i["name"].startswith("guard_only.xml")]
        ok("批次列表能看到仅上舰的批次", len(mine) == 1, [i["name"] for i in imports][:4])
        ok("批次里 guard_events=1 / gift_events=0",
           bool(mine) and mine[0]["guard_events"] == 1 and mine[0]["gift_events"] == 0,
           mine[0] if mine else None)
        if mine:
            code, r = _req("DELETE",
                           f"/api/rooms/{ROOM}/imports?source={quote(mine[0]['source'])}")
            ok("撤销成功且删掉 1 条上舰",
               code == 200 and r.get("guard_deleted") == 1, r)
            ok("上舰记录真的没了",
               q("SELECT COUNT(1) FROM guard_events WHERE source LIKE 'xml:guard_only.xml#%'")[0][0] == 0)

        # ── 27. 批次列表按最近时间倒序 ──
        print("[27] 批次排序")
        undo("sortA.xml")
        undo("sortB.xml")
        # 两份内容必须不同：内容相同会被 dedupe_key 判成同一个事件，批次根本不会产生
        old_xml = XML_SAME_A.replace("2024-12-01T18:05:02", "2024-10-01T18:05:02")                             .replace('uid="111"', 'uid="2111"')
        new_xml = XML_SAME_A.replace("2024-12-01T18:05:02", "2024-11-20T18:05:02")                             .replace('uid="111"', 'uid="3111"')
        post(f"/api/rooms/{ROOM}/import/danmaku-xml", old_xml, "sortA.xml")
        post(f"/api/rooms/{ROOM}/import/danmaku-xml", new_xml, "sortB.xml")
        imports = (_req("GET", f"/api/rooms/{ROOM}/imports")[1] or {}).get("imports") or []
        names = [i["name"] for i in imports]
        # 库里还有别的批次（比如 no_raw.xml 的场次更晚），所以只断言这两者的相对顺序
        ia = next((i for i, n in enumerate(names) if n.startswith("sortA.xml")), -1)
        ib = next((i for i, n in enumerate(names) if n.startswith("sortB.xml")), -1)
        ok("较晚的场次排在较早的前面", ia >= 0 and ib >= 0 and ib < ia, names[:5])
        last_ts = [i["last_ts"] for i in imports]
        ok("整张表按 last_ts 倒序", last_ts == sorted(last_ts, reverse=True), last_ts[:5])
        undo("sortA.xml")
        undo("sortB.xml")

        # ── 28. 普通用户不能导入 ──
        print("[28] 普通用户不能导入")
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

        # ── 29. 导入不能长时间独占写锁（实时链路会因此丢礼物） ──
        print("[29] 导入期间的写锁粒度")
        # 造一份 6 万条礼物的 XML：不分批提交的话整段导入全程独占写锁，
        # 实时链路 busy_timeout 一到就抛 database is locked，那条礼物就没了。
        n_gift = 60000
        body = [
            '<?xml version="1.0" encoding="utf-8"?>', "<i>",
            "<metadata><record_start_time>2024-09-01T20:00:00+08:00</record_start_time>"
            "</metadata>",
        ]
        for i in range(n_gift):
            body.append(f'<gift ts="{i % 7200}.{i % 1000:03d}" uid="{100000 + i}" '
                        f'user="U{i}" giftname="辣条" giftcount="1"/>')
        body.append("</i>")
        big_xml = "\n".join(body).encode()
        db_path = str(srv._room_db_path(ROOM))

        max_wait = [0.0]
        live_err: list[str] = []
        live_done = [False]

        def _live_writer() -> None:
            from app.storage import StatsStore
            st = StatsStore(db_path)
            while not live_done[0]:
                t0 = time.time()
                try:
                    st._conn.execute(
                        "INSERT INTO gift_events (ts,month,uid,uname,event_type,gift_name,"
                        "gift_num,is_blind_box,blind_box_cost,actual_value,profit_value,"
                        "raw_json,dedupe_key,source) "
                        "VALUES (?,?,'9','实时','SEND_GIFT','辣条',1,0,0,0,0,'{}','','')",
                        (int(time.time()), "2024-12"))
                    st._conn.commit()
                    max_wait[0] = max(max_wait[0], time.time() - t0)
                except Exception as exc:  # noqa: BLE001
                    live_err.append(f"{type(exc).__name__}: {exc}")
                time.sleep(0.002)
            st._conn.close()

        lw = threading.Thread(target=_live_writer, daemon=True)
        lw.start()
        time.sleep(0.05)
        t0 = time.time()
        res_big = srv._import_xml_blocking(
            ROOM, db_path, big_xml, "big.xml", "xml:big.xml#lockprobe",
            dict(srv._GIFT_PRICE_CACHE), False, False)
        import_s = time.time() - t0
        live_done[0] = True
        lw.join(timeout=10)

        ok("大文件导入成功", bool(res_big.get("ok")), res_big.get("error"))
        ok(f"6 万条全部落库", res_big.get("stats", {}).get("inserted") == n_gift,
           res_big.get("stats", {}).get("inserted"))
        ok("导入期间实时写入 0 次失败", not live_err, live_err[:3])
        # 时间阈值只作粗筛：同一台机器上抖动很大（实测 90~540ms），
        # 真正要守的是「实时写入不被挡住太久」和「导入是分批提交的」。
        # 结构性断言（下面那条）才是确定性的，改回整体事务它立刻变红。
        ok(f"单次写锁等待 < 1.5s（实测 {max_wait[0]*1000:.0f}ms，导入总计 {import_s:.2f}s）",
           max_wait[0] < 1.5, max_wait[0])
        src_srv = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        seg = src_srv[src_srv.index("def _import_xml_blocking"):]
        seg = seg[:seg.index("\n@app.")]
        import re as _re
        m_ce = _re.search(r"commit_every\s*=\s*(\d+)", seg)
        ok("导入确实是每 N 条提交一次（N 在 100~5000 之间）",
           bool(m_ce) and 100 <= int(m_ce.group(1)) <= 5000,
           m_ce.group(1) if m_ce else "没有 commit_every")
        # 这份大数据要清掉，别影响后面的用例
        from app.storage import StatsStore as _SS
        _st = _SS(db_path)
        _st.delete_gift_events_by_source("xml:big.xml#lockprobe")
        _st._conn.close()

        # ── 30. 事件处理异常不能静默消失 ──
        print("[30] 处理函数异常兜底")
        import logging as _logging
        from app.bot import LiveRobot

        recs: list[_logging.LogRecord] = []

        class _Cap(_logging.Handler):
            def emit(self, record):
                recs.append(record)

        cap = _Cap()
        _logging.getLogger("bili-live-robot").addHandler(cap)
        _logging.getLogger("bili-live-robot").setLevel(_logging.DEBUG)

        class _Stub:
            logger = _logging.getLogger("bili-live-robot")
            _dropped_stats = 0

        stub = _Stub()

        async def _boom(event):
            raise ValueError("坏掉的 payload")

        wrapped = LiveRobot._guarded(stub, _boom)
        import asyncio as _asyncio
        _asyncio.run(wrapped({"type": "SEND_GIFT_V2", "data": {"pb": "xx"}}))
        ok("异常被兜住，没有抛出", True)
        ok("失败计数 +1", stub._dropped_stats == 1, stub._dropped_stats)
        joined = "\n".join(r.getMessage() for r in recs if r.levelno >= _logging.ERROR)
        ok("日志里有处理函数名", "_boom" in joined, joined[-200:])
        ok("日志里有事件类型", "SEND_GIFT_V2" in joined, joined[-200:])
        _logging.getLogger("bili-live-robot").removeHandler(cap)

        # 写库被锁时 _record_stat 必须重试到成功，而不是丢掉
        class _Stub2(_Stub):
            pass

        tries = {"n": 0}
        stub2 = _Stub2()

        def _flaky() -> None:
            tries["n"] += 1
            if tries["n"] < 3:
                raise sqlite3.OperationalError("database is locked")

        ok("_record_stat 重试后成功", LiveRobot._record_stat(
            stub2, "礼物统计", label="probe", write=_flaky) is True)
        ok("确实重试了 3 次", tries["n"] == 3, tries["n"])
        ok("成功时不计入 dropped", stub2._dropped_stats == 0, stub2._dropped_stats)

        def _always_locked() -> None:
            raise sqlite3.OperationalError("database is locked")

        ok("始终失败时返回 False",
           LiveRobot._record_stat(stub2, "礼物统计", label="probe2", write=_always_locked) is False)
        ok("始终失败时 dropped +1", stub2._dropped_stats == 1, stub2._dropped_stats)

        # ── 31. 中文文件名的导入 ──
        print("[31] 中文文件名")
        # fetch 的 header 值只能是 ISO-8859-1，中文文件名直接塞进 X-Filename 会让浏览器
        # 抛 TypeError（请求根本发不出去）。前端 encodeURIComponent，后端 unquote。
        cn_name = "录播 2024年12月01日#1.xml"
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_SAME_A,
                       quote(cn_name, safe=""), dry_run=1)
        ok("百分号编码的中文文件名能被接受", code == 200 and r.get("ok"), r.get("error"))
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_SAME_A,
                       quote(cn_name, safe=""))
        ok("真导入成功", code == 200 and r.get("ok"), r.get("error"))
        imports = (_req("GET", f"/api/rooms/{ROOM}/imports")[1] or {}).get("imports") or []
        # `#` 会被替换成 `_`（否则 source 的 `名字#摘要` 会被切错），其余原样保留
        want = cn_name.replace("#", "_")
        hit = [i for i in imports if i["name"] == want]
        ok("批次名还原成中文（# 已替换）", bool(hit), [i["name"] for i in imports][:6])
        if hit:
            code, r = _req("DELETE", f"/api/rooms/{ROOM}/imports?source={quote(hit[0]['source'])}")
            ok("中文批次能撤销", code == 200 and r.get("ok"), r.get("error"))
        # 未编码的 ASCII 文件名照旧可用（脚本/老客户端）
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", XML_SAME_A, "plain.xml", dry_run=1)
        ok("未编码的 ASCII 文件名照旧可用", code == 200 and r.get("ok"), r.get("error"))

        # ── 32. 大库上的预检不能是全表扫描 ──
        print("[32] 预检性能（dedupe_key 部分索引）")
        # dedupe_key 上是部分索引（WHERE dedupe_key <> ''）。如果预检只查
        # `dedupe_key = ?`，SQLite 推不出索引条件 → 每条全表扫描 → O(n²)。
        # 实测 6 万行的库 + 3000 次查找要 23.7 秒（折算 20 万条预检 26 分钟）。
        from app.storage import GiftEvent as _GE
        from app.storage import StatsStore as _S2
        _s = _S2(str(db))
        _s._conn.execute("BEGIN")
        fill = 60000
        for i in range(fill):
            _s.record_gift_event(
                _GE(ts=1735664400 + i, month="2024-12", uid=700000 + i, uname=f"f{i}",
                    event_type="SEND_GIFT", gift_name="辣条", gift_num=1, is_blind_box=0,
                    blind_box_cost=0, actual_value=0, profit_value=0, raw_json="{}"),
                dedupe_key=f"fill{i}", source="xml:fill.xml#ffff", commit=False)
        _s._conn.commit()
        _s._conn.close()

        n_probe = 3000
        body = ['<?xml version="1.0" encoding="utf-8"?>', "<i>",
                "<metadata><record_start_time>2025-03-01T20:00:00+08:00</record_start_time>"
                "</metadata>"]
        for i in range(n_probe):
            body.append(f'<gift ts="{i}.000" uid="{900000 + i}" user="P{i}" '
                        f'giftname="辣条" giftcount="1"/>')
        body.append("</i>")
        probe_xml = "\n".join(body)
        t0 = time.time()
        code, r = post(f"/api/rooms/{ROOM}/import/danmaku-xml", probe_xml, "probe.xml", dry_run=1)
        dt = time.time() - t0
        ok(f"预检 {n_probe} 条用时 {dt:.2f}s（< 5s）", code == 200 and dt < 5, dt)
        ok("预检结果正确（全部是新增）",
           (r.get("stats") or {}).get("would_insert") == n_probe,
           (r.get("stats") or {}).get("would_insert"))

        # ── 33. 按日期删除要连上舰一起删 ──
        print("[33] 按日期删除包含上舰")
        from app.storage import StatsStore as _S3
        _s3 = _S3(str(db))
        _s3.record_guard_event(ts=1600000000, month="2020-09", uid=880001, uname="老舰长",
                               guard_level=3, guard_name="舰长", months=1,
                               price_coin=198000, value=1980, start_time=1600000000,
                               source="xml:old_guard.xml#0ld0")
        _s3.record_guard_event(ts=int(time.time()), month="2024-12", uid=880002, uname="新舰长",
                               guard_level=2, guard_name="提督", months=1,
                               price_coin=1998000, value=19980, start_time=int(time.time()),
                               source="xml:new_guard.xml#n3w0")
        _s3._conn.close()
        before = q("SELECT COUNT(1) FROM guard_events")[0][0]
        code, r = _req("POST", f"/api/rooms/{ROOM}/delete_old",
                       json.dumps({"date": "2021-01-01", "type": "gift"}).encode(),
                       {"Content-Type": "application/json"})
        ok("删除接口返回 200", code == 200, r)
        ok("返回里带上舰删除条数", (r or {}).get("deleted_guards") == 1, r)
        left = q("SELECT uid FROM guard_events ORDER BY ts")
        ok("2020 年的上舰被删掉、新的还在",
           len(left) == before - 1 and all(row[0] == 880002 for row in left),
           left[:5])

        # ── 34. 导入进行中撤销，聚合不能与明细脱节 ──
        print("[34] 并发撤销（导入还在写）")
        from app.storage import GiftEvent, StatsStore
        race_db = str(room_dir / "data" / "race.db")
        RSRC = "xml:race.xml#deadbeef"

        def _rev(i: int):
            return GiftEvent(ts=1735664400 + i, month="2024-12", uid=1001, uname="甲",
                             event_type="SEND_GIFT", gift_name="辣条", gift_num=1,
                             is_blind_box=0, blind_box_cost=0, actual_value=10,
                             profit_value=0, raw_json="{}")

        undo_store = StatsStore(race_db)
        stop_evt = [False]
        produced = {"n": 0}
        imp_err: list[str] = []

        def _importer() -> None:
            st = StatsStore(race_db)
            try:
                for i in range(40000):
                    if stop_evt[0]:
                        break
                    st.record_gift_event(_rev(i), dedupe_key=f"k{i}", source=RSRC,
                                         commit=False)
                    produced["n"] += 1
                    if produced["n"] % 2000 == 0:
                        st._conn.commit()
                        time.sleep(0.01)
                st._conn.commit()
            except Exception as exc:  # noqa: BLE001
                imp_err.append(f"{type(exc).__name__}: {exc}")
            finally:
                st._conn.close()

        ith = threading.Thread(target=_importer)
        ith.start()
        time.sleep(0.05)
        res_undo = undo_store.delete_gift_events_by_source(RSRC)
        stop_evt[0] = True
        ith.join(timeout=20)

        def _race_state():
            row = undo_store._conn.execute(
                "SELECT COUNT(1), COALESCE(SUM(actual_value), 0) FROM gift_events "
                "WHERE month='2024-12' AND uid=1001").fetchone()
            ag = undo_store._conn.execute(
                "SELECT COALESCE(gift_count,0), COALESCE(value_total,0) FROM monthly_gift_stats "
                "WHERE month='2024-12' AND uid=1001").fetchone()
            return (int(row[0]), int(row[1])), (int(ag[0]), int(ag[1])) if ag else (0, 0)

        detail_now, agg_now = _race_state()
        ok("撤销时没有抛错", not imp_err, imp_err[:2])
        ok("撤销删掉的行数 ≥ 0 且返回正常", res_undo.get("deleted", -1) >= 0, res_undo)
        # 关键断言：聚合必须与明细一致（修前会出现聚合仍记着 2000 条/20000 的幽灵数据）
        ok(f"撤销后聚合与明细一致（明细 {detail_now} / 聚合 {agg_now}）",
           detail_now == agg_now, {"detail": detail_now, "agg": agg_now})
        undo_store._conn.close()

        # ── 35. 统计自检与修复 ──
        print("[35] 统计自检 / 修复")
        drift_db = str(room_dir / "data" / "drift.db")
        ds = StatsStore(drift_db)
        for i in range(10):
            ds.record_gift_event(GiftEvent(ts=1735664400 + i, month="2024-12", uid=2002,
                                           uname="乙", event_type="SEND_GIFT",
                                           gift_name="辣条", gift_num=1, is_blind_box=0,
                                           blind_box_cost=0, actual_value=10,
                                           profit_value=0, raw_json="{}"),
                                 dedupe_key=f"d{i}", source="xml:drift.xml#d")
        # 人为制造「写了明细没写聚合」：只记 1 条、10 电池
        ds._conn.execute("UPDATE monthly_gift_stats SET gift_count=1, gift_num_total=1, "
                         "value_total=10 WHERE month='2024-12' AND uid=2002")
        # 再造一个「只剩聚合、明细已被保留期清掉」的月份
        ds._conn.execute(
            "INSERT INTO monthly_gift_stats (month, uid, uname, gift_count, gift_num_total,"
            " value_total, last_ts) VALUES ('2023-01', 3003, '丙', 5, 5, 50, 1600000000)")
        ds._conn.commit()

        rep = ds.consistency_report()
        ok("自检发现偏差", rep["ok"] is False and rep["gift_mismatch_count"] >= 1,
           rep["gift_mismatch"][:2])
        ok("自检指出偏差月份", "2024-12" in (rep.get("months") or []), rep.get("months"))

        fixed = ds.repair_stats()
        ok("重算后一致", fixed["after"]["ok"] is True, fixed["after"])
        row = ds._conn.execute(
            "SELECT gift_count, value_total FROM monthly_gift_stats "
            "WHERE month='2024-12' AND uid=2002").fetchone()
        ok("按明细重算成 10 条 / 100 电池", tuple(row) == (10, 100), row)
        kept = ds._conn.execute(
            "SELECT COUNT(1) FROM monthly_gift_stats WHERE month='2023-01'").fetchone()[0]
        ok("只剩聚合的月份不会被抹掉", kept == 1, kept)
        ds._conn.close()

        # ── 36. 老库里的连击汇总行（COMBO_SEND）不能污染统计 ──
        print("[36] COMBO_SEND 不计入统计")
        combo_db = str(room_dir / "data" / "combo.db")
        cs = StatsStore(combo_db)
        # 逐条礼物（正确数据）
        for i in range(3):
            cs.record_gift_event(GiftEvent(
                ts=1735664400 + i, month="2024-12", uid=4004, uname="戊",
                event_type="SEND_GIFT", gift_name="人气票", gift_num=1,
                is_blind_box=0, blind_box_cost=0, actual_value=10, profit_value=0,
                raw_json="{}"), dedupe_key=f"c{i}", source="xml:combo.xml#c")
        before_agg = cs._conn.execute(
            "SELECT gift_count, gift_num_total, value_total FROM monthly_gift_stats "
            "WHERE month='2024-12' AND uid=4004").fetchone()
        # 连击汇总（老版本会把它当礼物写进来）
        cs.record_gift_event(GiftEvent(
            ts=1735664500, month="2024-12", uid=4004, uname="戊",
            event_type="COMBO_SEND", gift_name="人气票", gift_num=20,
            is_blind_box=0, blind_box_cost=0, actual_value=0, profit_value=0,
            raw_json='{"combo_total_coin": 300}'), dedupe_key="combo1", source="")
        after_agg = cs._conn.execute(
            "SELECT gift_count, gift_num_total, value_total FROM monthly_gift_stats "
            "WHERE month='2024-12' AND uid=4004").fetchone()
        ok("COMBO_SEND 行写进明细但不进月度聚合",
           tuple(before_agg) == tuple(after_agg), {"before": tuple(before_agg),
                                                   "after": tuple(after_agg)})
        # 重算也只认 SEND_GIFT：否则修复会把 COMBO 行算回聚合
        cs.rebuild_monthly_stats(["2024-12"])
        rebuilt = cs._conn.execute(
            "SELECT gift_count, gift_num_total FROM monthly_gift_stats "
            "WHERE month='2024-12' AND uid=4004").fetchone()
        ok("重算后仍是 3 条（不含 COMBO_SEND）", tuple(rebuilt) == (3, 3), tuple(rebuilt))
        rep2 = cs.consistency_report()
        ok("自检认为一致（明细口径同样只认 SEND_GIFT）", rep2["ok"] is True,
           rep2["gift_mismatch"][:2])
        cs.close()

        # 接口层：管理员可用、普通用户 403
        code, r = _req("GET", f"/api/rooms/{ROOM}/stats_check")
        ok("自检接口返回 200", code == 200 and "gift_mismatch_count" in r, code)
        code, r = _req("POST", f"/api/rooms/{ROOM}/stats_repair")
        ok("重算接口返回 200 且修完一致", code == 200 and (r.get("after") or {}).get("ok") is True,
           (r.get("after") or {}).get("ok"))
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/rooms/{ROOM}/stats_repair", data=b"", method="POST",
            headers={"Cookie": "session=user-token"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                code2 = resp.status
        except urllib.error.HTTPError as exc:
            code2 = exc.code
        ok("普通用户不能重算统计", code2 == 403, code2)
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
