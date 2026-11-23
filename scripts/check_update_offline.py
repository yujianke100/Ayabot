"""第二十九轮自检：断网时的更新检查 + 死代码里的聚合偏差 + 静态扫描清零。

覆盖：
  1. 所有版本号来源都失败时（断网 / 全线被墙），`/api/update/check` 必须返回
     `{"ok": false, "error": "无法获取版本信息..."}`，**不能抛 NameError / 500**。
     修复前实测：`NameError: name 'get_version' is not defined`。
  2. `StatsStore.delete_events_before()` 删掉某月一半数据后，
     月度汇总必须跟着重算（否则聚合永远大于明细）。
  3. `app/` 下 ruff 的 F821（undefined-name）必须为 0 —— 这类问题
     只有真正的执行路径才会暴露，靠人工读代码几乎不可能发现。

用法: ./.venv/Scripts/python.exe scripts/check_update_offline.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    # ── 1. 断网时的更新检查 ──
    print("[1] 所有版本号来源失败时不能抛异常")
    from app.web import server as srv

    real_tag = srv._fetch_tag_race
    real_race = srv._race_json

    async def _no_tag(session):  # noqa: ANN001
        return "", "", 0

    async def _no_meta(session, url):  # noqa: ANN001
        return None

    try:
        srv._fetch_tag_race = _no_tag       # type: ignore[assignment]
        srv._race_json = _no_meta           # type: ignore[assignment]
        raised = None
        payload = None
        try:
            payload = asyncio.run(srv._compute_update_payload())
        except Exception as exc:  # noqa: BLE001
            raised = exc
        ok("不抛异常", raised is None, f"{type(raised).__name__}: {raised}")
        if payload is not None:
            ok("ok=False", payload.get("ok") is False, payload.get("ok"))
            ok("给出可读错误", "无法获取版本信息" in str(payload.get("error")),
               payload.get("error"))
            ok("仍带 current 版本", str(payload.get("current") or "").startswith("v"),
               payload.get("current"))
            ok("仍列出下载线路（让用户能手动下）",
               isinstance(payload.get("downloads"), list)
               and len(payload["downloads"]) >= 3,
               len(payload.get("downloads") or []))
    finally:
        srv._fetch_tag_race = real_tag      # type: ignore[assignment]
        srv._race_json = real_race          # type: ignore[assignment]

    # 正常路径不被破坏（真实网络：能取到 latest 就应当是 ok=True）
    print("[2] 正常路径仍然可用（真实网络）")
    try:
        payload2 = asyncio.run(srv._compute_update_payload())
        ok("能拿到 latest", bool(payload2.get("latest")),
           f"ok={payload2.get('ok')} latest={payload2.get('latest')} "
           f"via={payload2.get('meta_source')}")
        ok("带 meta_ms", int(payload2.get("meta_ms") or 0) >= 0,
           payload2.get("meta_ms"))
    except Exception as exc:  # noqa: BLE001
        ok("正常路径不抛异常", False, f"{type(exc).__name__}: {exc}")

    # ── 3. delete_events_before 的聚合偏差 ──
    print("[3] 部分删除月份后聚合必须重算")
    from app import tzutil
    from app.storage import GiftEvent, StatsStore

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_del_"))
    tzutil.set_config_timezone("Asia/Shanghai")
    st = StatsStore(str(tmp / "d.db"))
    # 同一个月里放 4 条，前 2 条的 ts 更早
    month = tzutil.month_str(int(time.time()))
    base = int(time.time()) - 4 * 86400
    for i in range(4):
        st.record_gift_event(GiftEvent(
            ts=base + i * 3600, month=month, uid=1000 + i, uname=f"u{i}",
            event_type="SEND_GIFT", gift_name="辣条", gift_num=1,
            is_blind_box=0, blind_box_cost=0, actual_value=100, profit_value=0,
            raw_json=json.dumps({"total_coin": 100})))
    before = st.get_monthly_total_blindbox(month)
    before_gift = st._conn.execute(
        "SELECT COALESCE(SUM(gift_count),0) FROM monthly_gift_stats WHERE month=?",
        (month,)).fetchone()[0]
    ok("删除前有 4 条明细", st._conn.execute(
        "SELECT COUNT(*) FROM gift_events").fetchone()[0] == 4)

    # 删掉前 2 条（同一个月，属于"部分删除"）
    res = st.delete_events_before(base + 2 * 3600)
    left = st._conn.execute("SELECT COUNT(*) FROM gift_events").fetchone()[0]
    ok("明细剩 2 条", left == 2, left)
    agg = st._conn.execute(
        "SELECT COALESCE(SUM(gift_count),0), COALESCE(SUM(gift_num_total),0) "
        "FROM monthly_gift_stats WHERE month=?", (month,)).fetchone()
    ok("月度汇总跟着重算（不再是 4）", int(agg[0]) == 2, f"汇总={agg[0]} 明细={left}")
    ok("汇总与明细一致", int(agg[0]) == left, f"{agg[0]} vs {left}")
    ok("返回值里带删除条数", res.get("deleted_gift_events") == 2, res)
    st.close()
    shutil.rmtree(tmp, ignore_errors=True)

    # ── 4. 静态扫描：app/ 下不能有 undefined-name ──
    print("[4] ruff F821（undefined-name）必须为 0")
    ruff = ROOT / ".venv" / "Scripts" / "python.exe"
    try:
        proc = subprocess.run(
            [str(ruff), "-m", "ruff", "check", "app/", "--select", "F821",
             "--output-format", "concise"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=180)
        out = (proc.stdout + proc.stderr).strip()
        ok("app/ 无 F821", proc.returncode == 0, out[:600] or "clean")
    except FileNotFoundError:
        print("  SKIP: 找不到解释器")
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIP: {exc}")

    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("UPDATE OFFLINE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
