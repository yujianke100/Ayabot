"""「删除旧数据」必须留退路。

`data/desktop 删除旧数据` / `撤销导入` / `重算统计` 都是**不可恢复**的：
用户选错一次日期，几年的礼物记录就没了，界面上没有任何后悔药。

现在删除前会用 SQLite 的 `Connection.backup()` 做一致性快照：
  - 机器人正在写也不会拷到半截数据（`copy2` 有这个问题）；
  - 备份落在 `<data>/backups/bot_<时间>_<动作>.db`，只保留最近 5 份；
  - 库太大（>300MB）就跳过并如实告诉用户，而不是假装备份成功。

    ./.venv/Scripts/python.exe scripts/check_destructive_backup.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UID = 777


def main() -> int:
    from app.storage import GiftEvent, StatsStore
    from app.web import server as srv

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="backup_"))
    db = tmp / "data" / "bot.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        st = StatsStore(str(db))
        now = int(time.time())
        for i in range(30):
            ts = now - (40 - i) * 86400
            st.record_gift_event(GiftEvent(
                ts=ts, month=time.strftime("%Y-%m", time.localtime(ts)),
                uid=UID, uname="老观众", event_type="SEND_GIFT", gift_name="辣条",
                gift_num=1, is_blind_box=0, blind_box_cost=0, actual_value=1,
                profit_value=0, raw_json=json.dumps({"total_coin": 100})), commit=False)
        st._conn.commit()

        print("[1] 备份能生成，而且是可用的库")
        got = st.backup_before_destructive("delete_old_gift")
        ok("返回了备份路径", bool(got), got)
        ok("文件真的存在", bool(got) and Path(got).exists())
        if got:
            import sqlite3
            b = sqlite3.connect(f"file:{got}?mode=ro", uri=True)
            n = b.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
            b.close()
            ok("备份里行数和原库一致（30）", n == 30, n)
            ok("备份放在 data/backups/ 下", "backups" in str(got), str(got))
        st.close()

        print("[2] 只保留最近 5 份（备份很占地方）")
        st = StatsStore(str(db))
        for i in range(8):
            st.backup_before_destructive(f"round{i}")
        files = list((db.parent / "backups").glob("bot_*.db"))
        ok("备份数量被夹到 5", len(files) <= 5, len(files))
        st.close()

        print("[3] 库太大时跳过备份，而不是假装成功")
        st = StatsStore(str(db))
        real_max = st.BACKUP_MAX_BYTES
        st.BACKUP_MAX_BYTES = 1          # 1 字节上限 → 一定跳过
        skipped = st.backup_before_destructive("too_big")
        st.BACKUP_MAX_BYTES = real_max
        ok("返回 None（调用方据此提示用户）", skipped is None, skipped)
        st.close()

        print("[4] delete_old 真的先备份再删")
        srv._ROOMS_BASE_DIR = str(tmp.parent)
        rooms = tmp.parent / "rooms" / "1946287911"
        (rooms / "data").mkdir(parents=True, exist_ok=True)
        shutil.copy(db, rooms / "data" / "bot.db")
        (rooms / "config.yaml").write_text(
            (ROOT / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        srv._ROOMS_BASE_DIR = str(tmp.parent)
        srv._ROOM_DB_PATH_CACHE.clear()

        cutoff = time.strftime("%Y-%m-%d", time.localtime(now - 10 * 86400))
        res = srv._delete_old_blocking(rooms / "data" / "bot.db", cutoff, "gift")
        ok("返回 backup_path", bool(res.get("backup_path")), res.get("backup_path"))
        ok("备份文件存在", bool(res.get("backup_path"))
           and Path(res["backup_path"]).exists())
        ok("确实删掉了数据", res.get("deleted_gifts", 0) > 0, res.get("deleted_gifts"))
        # 从备份里还能把数据捞回来
        if res.get("backup_path"):
            import sqlite3
            b = sqlite3.connect(f"file:{res['backup_path']}?mode=ro", uri=True)
            n = b.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
            b.close()
            ok("备份里是删除**前**的完整数据", n == 30, n)

        print("[5] 前端把备份路径显示给用户")
        src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        seg = src[src.index("async function confirmDelete"):]
        seg = seg[: seg.index("async function ", 20)]
        ok("读 data.backup_path", "backup_path" in seg)
        ok("没有备份时也如实说明", "未生成备份" in seg)
    finally:
        shutil.rmtree(tmp.parent if tmp.parent.name.startswith("backup_") else tmp,
                      ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("DESTRUCTIVE BACKUP OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
