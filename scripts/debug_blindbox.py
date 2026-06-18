"""排查盲盒 actual_value 问题"""
import json
import sqlite3
import sys

db_path = sys.argv[1] if len(sys.argv) > 1 else r"rooms\1946287911\data\bot.db"
uid = int(sys.argv[2]) if len(sys.argv) > 2 else 27950910

conn = sqlite3.connect(db_path)
cur = conn.cursor()

# 查指定用户所有盲盒事件
rows = cur.execute(
    """SELECT id, ts, gift_name, gift_num, blind_box_cost, actual_value, profit_value,
              json_extract(raw_json, '$.num') as raw_num,
              json_extract(raw_json, '$.gift_num') as raw_gift_num,
              json_extract(raw_json, '$.blind_gift.gift_tip_price') as tip_price,
              json_extract(raw_json, '$.blind_gift.original_gift_price') as orig_price,
              json_extract(raw_json, '$.total_coin') as total_coin
       FROM gift_events
       WHERE uid = ? AND is_blind_box = 1
       ORDER BY ts DESC LIMIT 50
    """, (uid,)
).fetchall()

print(f"用户 {uid} 最近 50 条盲盒记录:\n")
print(f"{'id':<6} {'ts':<12} {'gift_name':<12} {'num':<4} {'raw_num':<8} {'gift_num':<9} {'cost':<6} {'actual':<7} {'profit':<7} {'tip_price':<10} {'orig_price':<11} {'total_coin':<10}")
print("-" * 110)
for r in rows:
    print(f"{r[0]:<6} {r[1]:<12} {r[2]:<12} {r[3]:<4} {str(r[7]):<8} {str(r[8]):<9} {r[4]:<6} {r[5]:<7} {r[6]:<7} {str(r[9]):<10} {str(r[10]):<11} {str(r[11]):<10}")

# 汇总
cost_sum = sum(r[4] for r in rows)
actual_sum = sum(r[5] for r in rows)
profit_sum = sum(r[6] for r in rows)
print(f"\n汇总: cost={cost_sum}, actual={actual_sum}, profit={profit_sum}")

# 检查 gift_tip_price 是单价还是总价
print("\n\n=== 分析 gift_tip_price 语义 ===")
for r in rows:
    tip = r[9]
    rnum = r[7]
    if tip and int(tip) > 0 and rnum and int(rnum) > 0:
        per_item = int(tip) // 100  # 金瓜子→电池
        total = per_item * int(rnum)
        stored = r[5]
        print(f"  {r[2]}: tip={tip}金瓜子, num={rnum}, 单价={per_item}电池, num倍={total}电池, DB存储={stored}电池, "
              f"{'✓ 匹配num倍' if stored == total else ('✓ 匹配单价' if stored == per_item else '✗ 都不匹配')}")

conn.close()
