"""
修复盲盒 actual_value 历史数据。
旧代码中 _extract_blindbox_profit 未将 gift_tip_price 乘以 num，
导致批量相同礼物时 actual_value 只记了 1 个的价格。

从 raw_json 中提取 num 和 gift_tip_price 重新计算并更新 DB。
"""
import json
import sqlite3
import sys
from pathlib import Path


def repair_db(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # 查出所有盲盒事件
    rows = cur.execute(
        "SELECT id, gift_num, blind_box_cost, raw_json FROM gift_events WHERE is_blind_box = 1"
    ).fetchall()

    fixed = 0
    for row in rows:
        event_id, gift_num, old_cost, raw_json_str = row
        if not raw_json_str:
            continue
        try:
            raw = json.loads(raw_json_str)
        except json.JSONDecodeError:
            continue

        num = int(raw.get("num") or raw.get("gift_num") or gift_num or 1)
        blind_dict = raw.get("blind_gift")
        if not isinstance(blind_dict, dict):
            continue

        # gift_tip_price 是单个物品价格（金瓜子），×num 得总价，÷100 转电池
        tip_price = int(blind_dict.get("gift_tip_price") or 0)
        if tip_price == 0:
            # 尝试其他字段
            tip_price = int(raw.get("total_coin") or 0)
        if tip_price == 0:
            continue

        # actual_value 单位是电池（金瓜子÷100），乘以 num 得总价
        new_actual = tip_price // 100 * num
        new_profit = new_actual - old_cost

        cur.execute(
            "UPDATE gift_events SET actual_value = ?, profit_value = ? WHERE id = ?",
            (new_actual, new_profit, event_id),
        )
        fixed += 1

    conn.commit()
    conn.close()
    return fixed


def find_db_paths(base_dir: str) -> list[Path]:
    """扫描 rooms/ 目录下所有 bot.db"""
    base = Path(base_dir).resolve()
    rooms_dir = base / "rooms"
    if not rooms_dir.exists():
        print(f"未找到 rooms 目录: {rooms_dir}")
        return []
    dbs = []
    for room_dir in sorted(rooms_dir.iterdir()):
        if room_dir.is_dir():
            db = room_dir / "data" / "bot.db"
            if db.exists():
                dbs.append(db)
    return dbs


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "."
    dbs = find_db_paths(base)
    if not dbs:
        dbs = [Path(base) / "data" / "bot.db"]
        if not dbs[0].exists():
            print(f"未找到数据库文件。用法: python repair_blindbox_values.py <项目根目录>")
            sys.exit(1)

    total = 0
    for db in dbs:
        n = repair_db(db)
        if n:
            print(f"  {db}: 修复 {n} 行")
        total += n
    print(f"总计: 修复 {total} 行")
