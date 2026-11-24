"""并发写库实测。

三个真实场景（用户会遇到的，不是假想）：
A. 同一份 XML 被点两次导入（连点两下、或者两个浏览器标签）
B. 两份不同的 XML 同时导入（两个房间管理员 / 用户自己开两个标签）
C. 导入的同时机器人还在实时收礼物（这是最要紧的：实时礼物不能丢）

判据：
- A 必须幂等：最终条数 = 单次导入的条数，不翻倍
- B 两份都要进去，且都不能报 database is locked
- C 实时礼物一条都不能丢；导入也不能因为锁冲突失败

    ./.venv/Scripts/python.exe scripts/check_concurrent_writes.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def make_xml(room: int, start_ts: int, n: int, uid_base: int, name_prefix: str) -> bytes:
    """一份最小但真实的 B站 录播弹幕 XML（录播姬 BililiveRecorder 格式）。

    礼物是 `<gift uid= giftname= giftcount= ts= user= raw=/>`，
    **不是** `<d>` 里的 JSON —— 后者只会被算成普通弹幕。
    """
    head = ('<?xml version="1.0" encoding="UTF-8"?><i>'
            '<chatserver>chat.bilibili.com</chatserver>'
            f'<chatid>{room}</chatid><mission>0</mission><maxlimit>100000</maxlimit>'
            '<state>0</state><real_name>0</real_name><source>k-v</source>')
    # 解析器需要一条 <d> 来推导录制起点时间（base），否则礼物时间戳全是 0
    items = [f'<d p="0.000,1,25,16777215,{start_ts * 1000},0,abc,0,0">开场</d>']
    for i in range(n):
        uid = uid_base + (i % 7)
        rel = float(i)
        raw = json.dumps({"cmd": "SEND_GIFT",
                          "data": {"uid": uid, "uname": f"{name_prefix}{uid}",
                                   "giftName": "辣条", "num": 1, "total_coin": 100}},
                         ensure_ascii=False).replace('"', "&quot;")
        items.append(f'<gift uid="{uid}" giftname="辣条" giftcount="1" '
                     f'ts="{rel:.3f}" user="{name_prefix}{uid}" raw="{raw}"/>')
    return (head + "".join(items) + "</i>").encode("utf-8")


def main() -> int:
    import asyncio
    import threading as th

    import uvicorn
    from app.web import server as srv

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="conc_"))
    try:
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (tmp / "data").mkdir(parents=True)
        (tmp / "config.yaml").write_text(example, encoding="utf-8")
        room = tmp / "rooms" / "1946287911"
        (room / "data").mkdir(parents=True)
        (room / "config.yaml").write_text(example, encoding="utf-8")

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._API_TOKEN = "t"
        srv._SESSIONS["t"] = (time.time() + 3600, "t", "admin", [])
        srv._rate_limited = lambda ip: False  # type: ignore[assignment]
        srv._GIFT_CACHE_BUILT = True
        srv._GIFT_PRICE_CACHE = {"辣条": (100, "silver")}

        db_path = room / "data" / "bot.db"
        from app.storage import GiftEvent, StatsStore

        def count() -> int:
            c = StatsStore(str(db_path))
            n = c._conn.execute("SELECT COUNT(1) FROM gift_events").fetchone()[0]
            c._conn.close()
            return int(n)

        base_ts = 1770000000
        xml_same = make_xml(1946287911, base_ts, 1200, 1000, "甲")
        xml_a = make_xml(1946287911, base_ts + 5000, 800, 2000, "乙")
        xml_b = make_xml(1946287911, base_ts + 9000, 800, 3000, "丙")

        def run_import(raw: bytes, name: str, out: dict, key: str) -> None:
            """线程里跑导入（不用 HTTP，直接调阻塞函数，等价于 to_thread 里跑）。"""
            try:
                res = srv._import_xml_blocking(
                    "1946287911", str(db_path), raw, name,
                    f"xml:{name}#{abs(hash(name)) % 10**8}", dict(srv._GIFT_PRICE_CACHE),
                    False, False)
                out[key] = res
            except Exception as exc:  # noqa: BLE001
                out[key] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        print("[A] 同一份 XML 并发导入两次：必须幂等")
        out: dict = {}
        th1 = threading.Thread(target=run_import, args=(xml_same, "same.xml", out, "1"))
        th2 = threading.Thread(target=run_import, args=(xml_same, "same.xml", out, "2"))
        th1.start(); th2.start(); th1.join(); th2.join()
        n_after_a = count()
        both_ok = all(v.get("ok") for v in out.values())
        ok("两次都没报错", both_ok, str({k: v.get('error') for k, v in out.items() if not v.get('ok')}))
        ok("最终只有 1200 条（没翻倍）", n_after_a == 1200, f"{n_after_a} 条")

        print("[B] 两份不同的 XML 同时导入：都要进去")
        out2: dict = {}
        t1 = threading.Thread(target=run_import, args=(xml_a, "a.xml", out2, "a"))
        t2 = threading.Thread(target=run_import, args=(xml_b, "b.xml", out2, "b"))
        t1.start(); t2.start(); t1.join(); t2.join()
        n_after_b = count()
        ok("两次都成功", all(v.get("ok") for v in out2.values()),
           str({k: v.get('error') for k, v in out2.items() if not v.get('ok')}))
        ok("总数 = 1200 + 800 + 800", n_after_b == 2800, f"{n_after_b} 条")

        print("[C] 导入的同时实时礼物在写：实时的一条都不能丢")
        stop = threading.Event()
        live_errors: list[str] = []
        live_written = [0]

        def live_writer() -> None:
            """模拟房间机器人：每 5ms 写一条实时礼物，持续到 stop。"""
            try:
                store = StatsStore(str(db_path))
            except Exception as exc:  # noqa: BLE001
                live_errors.append(f"connect: {exc}")
                return
            i = 0
            try:
                while not stop.is_set():
                    i += 1
                    ev = GiftEvent(
                        ts=int(time.time()), month=StatsStore.month_of_ts(int(time.time())),
                        uid=900000 + i, uname=f"实时{i}", event_type="SEND_GIFT",
                        gift_name="辣条", gift_num=1, is_blind_box=False,
                        blind_box_cost=0, actual_value=100, profit_value=0,
                        raw_json='{"total_coin": 100}')
                    try:
                        if store.record_gift_event(ev):
                            live_written[0] += 1
                    except Exception as exc:  # noqa: BLE001
                        live_errors.append(f"{type(exc).__name__}: {exc}")
                    time.sleep(0.005)
            finally:
                try:
                    store._conn.close()
                except Exception:
                    pass

        xml_big = make_xml(1946287911, base_ts + 20000, 4000, 4000, "丁")
        writer = threading.Thread(target=live_writer, daemon=True)
        writer.start()
        time.sleep(0.3)
        out3: dict = {}
        run_import(xml_big, "big.xml", out3, "big")
        time.sleep(0.5)
        stop.set()
        writer.join(timeout=10)
        n_final = count()
        ok("导入成功", bool(out3.get("big", {}).get("ok")), str(out3)[:160])
        ok("实时写入没有报锁错误", not live_errors, str(live_errors[:3]))
        ok("实时礼物一条没丢", live_written[0] > 0 and not live_errors, f"写入 {live_written[0]} 条")
        ok("导入的 4000 条全进去了",
           n_final == 2800 + 4000 + live_written[0], f"{n_final} vs 期望 {2800 + 4000 + live_written[0]}")

        print("[D] WAL 模式 / busy_timeout 是否真的开着")
        c = StatsStore(str(db_path))
        mode = c._conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = c._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        c._conn.close()
        ok("busy_timeout >= 5000", int(busy) >= 5000, f"{busy} ms")
        print(f"  journal_mode = {mode}（{ 'WAL，读写可并发' if str(mode).lower()=='wal' else '非 WAL：读会阻塞写' }）")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("CONCURRENT WRITES OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
