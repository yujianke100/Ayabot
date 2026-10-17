"""礼物统计链路自测：真实 SEND_GIFT_V2 报文 → 解码 → 盲盒盈亏 → 入库聚合。

样本来自 2026-10 实测抓包（scripts/sniff 抓的线上直播间事件）：
  - SAMPLE_LOGGED：登录态「心动盲盒 → 星光铃铛」事件，盲盒 50 电池 / 爆出 52 电池
  - SAMPLE_ANON  ：匿名连接下的事件，B 站抹掉 uid（uname 形如「让***」）

用法: ./.venv/Scripts/python.exe scripts/check_gift_v2.py
"""
from __future__ import annotations

import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bot import _extract_blindbox_profit  # noqa: E402
from app.gift_v2 import decode_send_gift_v2  # noqa: E402
from app.storage import GiftEvent, StatsStore  # noqa: E402

SAMPLE_LOGGED = (
    "CNfCjj0SDOatoumxvOS4jumypBpKaHR0cHM6Ly9pMS5oZHNsYi5jb20vYmZzL2ZhY2UvNzJjNmEwNDFhOTVjZDczODNkMmEwYzQyNTZmZGIwNG"
    "E2NDAyMmE5OC5qcGdCKgjmn7gMKB8yCeeMq+eMq+azpTjVkLQBQNWQtAFI/7f2BFD/0Z8DWAFgA0ogCJABEIaTAhoM5bm46L+Q55uy55uSKgbn"
    "iIblh7owiCdS9wQIiJMCEgzmmJ/lhYnpk4Ppk5sYASACKNAoMNAoOIgnQgRnb2xkShM0ODI4Nzk3OTIxMjU0MjA1NDQwULKzzNYGWAFiJDZhOG"
    "IwMDlmLTcxYWQtNGM2Yi04NzdkLTc3NWFhMDQ2MmNmY2gKcNAoeAWFAQAAgD+IAQGSAQbmipXlloLAAaifpwrqARUKDeWNl+eUuk5pZ2h0aW4Q"
    "yfvg+QSKAuEBCMn74PkEEtgBCg3ljZfnlLpOaWdodGluEkpodHRwczovL2kxLmhkc2xiLmNvbS9iZnMvZmFjZS9kNTJiMmUzNTNiMDg2OThhYz"
    "Y2YmM2ZTQ1YmU0NGQwYWIwMGVhOGNlLmpwZzJbCg3ljZfnlLpOaWdodGluEkpodHRwczovL2kxLmhkc2xiLmNvbS9iZnMvZmFjZS9kNTJiMmUz"
    "NTNiMDg2OThhYzY2YmM2ZTQ1YmU0NGQwYWIwMGVhOGNlLmpwZzoeCAcSGmJpbGliaWxp55u05pKt6auY6IO95Li75pKtkgIAmgLlAQpKaHR0cH"
    "M6Ly9zMS5oZHNsYi5jb20vYmZzL2xpdmUvYTJlODI1NGY1YzUzM2E5YjNmMDAyMDFhODg3OGMzN2ZmZmQ0MDY1MS5wbmcSS2h0dHBzOi8vaTAu"
    "aGRzbGIuY29tL2Jmcy9saXZlL2ZjNWVlOWNiYjAzNWVmOTRlNjQyZDNjNjc2Zjc0MWZjMzFlNThkMjcud2VicCpKaHR0cHM6Ly9pMC5oZHNsYi"
    "5jb20vYmZzL2xpdmUvYWU2YTkxMDZmZDY3MzdiNjg4YmI5M2EyOTM0NmUyNDA1ZjA1ZDcxOC5naWagAtAoqgIAWAFqAgggerECCNfCjj0SwwEK"
    "DOatoumxvOS4jumypBJKaHR0cHM6Ly9pMS5oZHNsYi5jb20vYmZzL2ZhY2UvNzJjNmEwNDFhOTVjZDczODNkMmEwYzQyNTZmZGIwNGE2NDAyMm"
    "E5OC5qcGcyWgoM5q2i6bG85LiO6bKkEkpodHRwczovL2kxLmhkc2xiLmNvbS9iZnMvZmFjZS83MmM2YTA0MWE5NWNkNzM4M2QyYTBjNDI1NmZk"
    "YjA0YTY0MDIyYTk4LmpwZzoLIP///////////wEaZAoJ5Y2h5ZW15oGwEAMYjq3yAiCOrfICKI6t8gIwjq3yAkgBUMn74PkEYA16CSM1NzYyQT"
    "c5OYIBCSM1NzYyQTc5OYoBCSM1NzYyQTc5OZIBByNGRkZGRkaaAQkjNTc2MkE3RTY="
)

SAMPLE_ANON = (
    "EgborqkqKioaSmh0dHBzOi8vaTEuaGRzbGIuY29tL2Jmcy9mYWNlL2VmODBmNmVhOWRlNGE4MjkxNmViZjczODUzZGFiMGUyMzhiNzk5ZDMuan"
    "BnQigI95ibRSgaMgnmlaPkv67lrpc4zKoYQMyqGEjxkaIDUP/RnwNYAWADUr4FCLzzARIP57KJ5Lid5Zui54Gv54mMGAEgAShkMGQ4ZEIEZ29s"
    "ZEoTNDgyODc5NjQ2NTQ2NjAyNjQ5NlDXsMzWBlgBYlViYXRjaDpnaWZ0OmNvbWJvX2lkOmVlMjIzOGQ5MjBjNmE2YzkwNjIzYmE2MWVkOTRlMW"
    "U5OjEzMjkwODU4OTc6MzExNjQ6MTc5MjIxOTIyMy4xMTA0aApwZHgFhQEAAIA/iAEBkgEG5oqV5ZaCwAHynqcK6gEVCg3ljZfnlLpOaWdodGlu"
    "EMn74PkEigLhAQjJ++D5BBLYAQoN5Y2X55S6TmlnaHRpbhJKaHR0cHM6Ly9pMS5oZHNsYi5jb20vYmZzL2ZhY2UvZDUyYjJlMzUzYjA4Njk4YW"
    "M2NmJjNmU0NWJlNDRkMGFiMDBlYThjZS5qcGcyWwoN5Y2X55S6TmlnaHRpbhJKaHR0cHM6Ly9pMS5oZHNsYi5jb20vYmZzL2ZhY2UvZDUyYjJl"
    "MzUzYjA4Njk4YWM2NmJjNmU0NWJlNDRkMGFiMDBlYThjZS5qcGc6HggHEhpiaWxpYmlsaeebtOaSremrmOiDveS4u+aSrZICBwiM4NcCEAGaAu"
    "UBCkpodHRwczovL3MxLmhkc2xiLmNvbS9iZnMvbGl2ZS9lMDUxZGZkNDU1NzY3OGY4ZWRjYWM0OTkzZWQwMGEwOTM1Y2JkOWNjLnBuZxJLaHR0"
    "cHM6Ly9pMC5oZHNsYi5jb20vYmZzL2xpdmUvMzJiNzk5MTIwZTE2MTRmYTYyNzViNmQxNWRhN2E1MmIyMWRkMDE5ZC53ZWJwKkpodHRwczovL2"
    "kwLmhkc2xiLmNvbS9iZnMvbGl2ZS84MTZmOGI3YWEyMTMyODg4ZmNlOTI4Y2RmYjE3YjljZjIxY2MwODIzLmdpZqoCFAgBEgcI9bfwAhACEgcI"
    "jODXAhABWAFqAggdeqECErcBCgborqkqKioSSmh0dHBzOi8vaTEuaGRzbGIuY29tL2Jmcy9mYWNlL2VmODBmNmVhOWRlNGE4MjkxNmViZjczOD"
    "UzZGFiMGUyMzhiNzk5ZDMuanBnMlQKBuiuqSoqKhJKaHR0cHM6Ly9pMS5oZHNsYi5jb20vYmZzL2ZhY2UvZWY4MGY2ZWE5ZGU0YTgyOTE2ZWJm"
    "NzM4NTNkYWIwZTIzOGI3OTlkMy5qcGc6CyD///////////8BGmUKCeWNoeWVteaBsBATGKS6ngYgpLqeBiikup4GMKS6ngZIAVDJ++D5BGDeCn"
    "oJI0M3NzBBNDk5ggEJI0M3NzBBNDk5igEJI0M3NzBBNDk5kgEHI0ZGRkZGRpoBCSNDNzcwQTRFNg=="
)


def _make_multi_payload() -> str:
    """构造「一个事件打包两件盲盒礼物」的报文（盲盒批量场景）。"""

    def varint(n: int) -> bytes:
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    def tag(field: int, wire: int) -> bytes:
        return varint((field << 3) | wire)

    def s(field: int, text: str) -> bytes:
        raw = text.encode()
        return tag(field, 2) + varint(len(raw)) + raw

    def i(field: int, value: int) -> bytes:
        return tag(field, 0) + varint(value)

    def item(name: str, num: int, price: int, total: int) -> bytes:
        return s(2, name) + i(3, num) + i(5, price) + i(7, total)

    blind = s(3, "心动盲盒") + i(6, 15000)
    msg = i(1, 123456) + s(2, "测试用户")
    msg += tag(9, 2) + varint(len(blind)) + blind
    for name, num, price, total in (("爱心抱枕", 1, 16000, 15000), ("棉花糖", 3, 9000, 45000)):
        body = item(name, num, price, total)
        msg += tag(10, 2) + varint(len(body)) + body
    return base64.b64encode(msg).decode()


def main() -> int:
    failures: list[str] = []

    # 1) 登录态盲盒样本
    gifts = decode_send_gift_v2(SAMPLE_LOGGED)
    if len(gifts) != 1:
        failures.append(f"logged sample decoded {len(gifts)} gifts, expected 1")
    else:
        g = gifts[0]
        profit = _extract_blindbox_profit(g)
        print("logged sample:", g["uid"], g["uname"], g["giftName"], "num=", g["num"], profit)
        if g["uid"] != 128164183:
            failures.append(f"uid wrong: {g['uid']}")
        if (g["giftName"], g["num"], g["price"], g["total_coin"]) != ("星光铃铛", 1, 5200, 5000):
            failures.append("logged sample fields wrong")
        # 盲盒：cost = 爆出前盲盒单价 5000/100 = 50，actual = 爆出礼物 5200/100 = 52
        if (profit["blind_box_cost"], profit["actual_value"], profit["profit_value"]) != (50, 52, 2):
            failures.append(f"blindbox math wrong: {profit}")

    # 2) 匿名事件：uid 必须解码为 0（上层据此丢弃并告警）
    anon = decode_send_gift_v2(SAMPLE_ANON)
    print("anon sample:", [(g["uid"], g["uname"], g["giftName"]) for g in anon])
    if not anon:
        failures.append("anon sample failed to decode")
    elif anon[0]["uid"] != 0:
        failures.append(f"anonymous uid should be 0, got {anon[0]['uid']}")

    # 3) 一个事件多件礼物（盲盒批量）
    multi = decode_send_gift_v2(_make_multi_payload())
    print("multi sample:", [(g["uid"], g["uname"], g["giftName"], g["num"]) for g in multi])
    if len(multi) != 2:
        failures.append(f"multi decoded {len(multi)}, expected 2")
    else:
        b1 = _extract_blindbox_profit(multi[0])
        b2 = _extract_blindbox_profit(multi[1])
        print("  blind1:", b1, "blind2:", b2)
        if (b1["blind_box_cost"], b1["actual_value"]) != (150, 160):
            failures.append(f"blind1 math wrong: {b1}")
        if (b2["blind_box_cost"], b2["actual_value"]) != (450, 270):
            failures.append(f"blind2 math wrong: {b2}")

    # 4) 入库 + 月聚合（get_user_monthly_blindbox 读的是 upsert 聚合表）
    with tempfile.TemporaryDirectory() as tmp:
        store = StatsStore(str(Path(tmp) / "bot.db"))
        month = "2026-10"
        for idx in range(2):
            profit = _extract_blindbox_profit(multi[idx])
            store.record_gift_event(
                GiftEvent(
                    ts=1792219000 + idx, month=month, uid=123456, uname="测试用户",
                    event_type="SEND_GIFT", gift_name=multi[idx]["giftName"],
                    gift_num=multi[idx]["num"], is_blind_box=1,
                    blind_box_cost=profit["blind_box_cost"],
                    actual_value=profit["actual_value"],
                    profit_value=profit["profit_value"], raw_json="{}",
                )
            )
        row = store.get_user_monthly_blindbox(month, 123456)
        print("aggregate:", row)
        # blind_box_count 累加的是「盲盒个数」(gift_num)：1 + 3 = 4
        if tuple(row) != (4, 600, 430, -170):
            failures.append(f"aggregate wrong: {row}")
        store.close()

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
