"""常驻后台任务的「监督覆盖率」：每个 create_task 都必须有人管。

`asyncio.create_task(常驻协程)` 起的任务**没有人 await**。里面抛一次异常它
就永久消失，而进程活着、心跳在更新、WebUI 显示「运行中」——
用户看到的是「机器人开着但没反应」，只有重启才行。

`_background_guard_loop` 会重建 `_BG_TASKS` 里登记过的任务。所以真正的风险是
**新加了一个常驻任务却忘了登记**：没有任何报错，只是那个功能悄悄永久失效。

本脚本枚举源码里所有 `self._xxx_task = asyncio.create_task(...)`，
逐个断言它要么在 `_BG_TASKS` 里，要么在允许豁免的清单里（并说明理由）。

    ./.venv/Scripts/python.exe scripts/check_bg_task_supervision.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: 允许不被 `_background_guard_loop` 监督的任务（附理由）
EXEMPT = {
    "_danmaku_task": "由 _danmaku_guard_loop 专门监督（要看连接是否假死，逻辑不同）",
    "_bg_guard_task": "没人监督监督者；它的存活由心跳文件间接体现",
}


def main() -> int:
    from app.bot import LiveRobot

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    src = (ROOT / "app/bot.py").read_text(encoding="utf-8")

    # 源码里所有 self._xxx_task = asyncio.create_task(...)
    started = set(re.findall(r"self\.(_\w+_task)\s*=\s*asyncio\.create_task\(", src))
    print(f"源码里的常驻任务：{sorted(started)}")

    supervised = {attr for attr, _factory in LiveRobot._BG_TASKS}
    print(f"_BG_TASKS 登记：{sorted(supervised)}")
    print()

    print("[1] 每个 create_task 都必须有归属")
    for attr in sorted(started):
        if attr in supervised:
            ok(f"{attr} 已被监督", True)
        elif attr in EXEMPT:
            print(f"  --   {attr} 豁免（{EXEMPT[attr]}）")
        else:
            ok(f"{attr} 没有监督", False, "既不在 _BG_TASKS 也不在豁免清单")

    print("[2] _BG_TASKS 里登记的 attr 必须真的存在且能被重建")
    for attr, factory in LiveRobot._BG_TASKS:
        ok(f"{attr} 在源码里被启动过", attr in started, "登记了但没有启动点")
        ok(f"{factory} 是 LiveRobot 的方法", hasattr(LiveRobot, factory), f"缺少 {factory}")

    print("[3] 监督清单里不能有重复")
    ok("没有重复登记", len(supervised) == len(LiveRobot._BG_TASKS))

    print("[4] 关键任务必须在表里")
    for must in ("_msg_worker_task", "_heartbeat_task", "_danmaku_guard_task"):
        ok(f"{must} 被监督", must in supervised,
           "消息/心跳/弹幕健康这三项任一失效，机器人就变成「开着但没反应」")

    print("[5] 重建逻辑的关键前提还在")
    body = src[src.index("def _check_background_tasks"):]
    body = body[:body.index("def _danmaku_silent_seconds")]
    ok("正常返回的任务不重建（否则每 30 秒空转起任务）",
       "if exc is None:" in body and "continue" in body)
    ok("有重建限流窗口", "_BG_RESTART_WINDOW" in body and "_BG_MAX_RESTARTS" in body)
    ok("放弃重建时上报 bg_dead", "_bg_dead.append" in body)

    print("[6] 心跳里要能看到监督状态")
    ok("心跳含 bg_restarts", '"bg_restarts"' in src)
    ok("心跳含 bg_dead", '"bg_dead"' in src)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("BG TASK SUPERVISION OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
