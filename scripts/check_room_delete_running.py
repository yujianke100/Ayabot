"""删除房间 × 同进程 Bot：删之前必须真的把 Bot 停掉。

`_use_inprocess()` 为真（**冻结 exe 就是这种模式**，也就是绝大多数 Windows 用户）
时，房间 Bot 是**当前进程里的一个 asyncio Task**，它持有 `bot.db` 的连接。

`clean_room()` 是同步函数，它只做 `_inproc_bots.pop(room_id)` ——
把记账条目删掉，**既没有 cancel Task，也没有调 robot.shutdown()**。
随后 `api_delete_room` 立刻 `shutil.rmtree(room_dir)`：

- Windows：`bot.db` / `bot.log` 还开着 → rmtree 抛 PermissionError →
  接口 500，目录删了一半，界面上房间还在但点进去报错
- 即使删成功：Bot Task 还活着，继续往一个已删除的目录写统计/日志

判据（这条自检守的就是它）：
  调用 `clean_room_async(room_id)` 之后，`_inproc_bots` 里不能再有它，
  而且那个 Task **必须已经被取消或结束**（不是「记账忘掉但还在跑」），
  并且 `robot.shutdown()` / `auth.stop()` 都真的被调到。

    ./.venv/Scripts/python.exe scripts/check_room_delete_running.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROOM = "1946287911"


def main() -> int:
    from app import process_manager as pm

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="rmroom_"))
    try:
        pm.set_rooms_base_dir(str(tmp))
        room_dir = tmp / "rooms" / ROOM
        (room_dir / "data").mkdir(parents=True, exist_ok=True)
        (room_dir / "config.yaml").write_text(
            (ROOT / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")

        from app.storage import GiftEvent, StatsStore

        async def scenario() -> dict:
            store = StatsStore(str(room_dir / "data" / "bot.db"))
            store.record_gift_event(GiftEvent(
                ts=1770000000, month=StatsStore.month_of_ts(1770000000), uid=1,
                uname="a", event_type="SEND_GIFT", gift_name="辣条", gift_num=1,
                is_blind_box=False, blind_box_cost=0, actual_value=100,
                profit_value=0, raw_json='{"total_coin":100}'))

            stop = asyncio.Event()

            async def fake_bot() -> None:
                # 模拟一个正在跑的 Bot：持有连接，一直在循环
                try:
                    while not stop.is_set():
                        await asyncio.sleep(0.05)
                finally:
                    pass

            class FakeRobot:
                shut_down = False

                async def shutdown(self) -> None:
                    FakeRobot.shut_down = True
                    stop.set()

            task = asyncio.create_task(fake_bot())
            holder = {"robot": FakeRobot()}

            class FakeAuth:
                stopped = False

                async def stop(self) -> None:
                    self.stopped = True

            auth = FakeAuth()
            pm._inproc_bots[ROOM] = (holder, auth, task)
            await asyncio.sleep(0.1)

            before = pm._inproc_is_running(ROOM)

            # ← 被测函数（删房间走的就是 async 版：先停 Bot 再删文件）
            await pm.clean_room_async(ROOM)
            await asyncio.sleep(0.15)

            return {
                "before": before,
                "task_done": task.done(),
                "task_cancelled": task.cancelled(),
                "robot_shutdown": FakeRobot.shut_down,
                "auth_stopped": auth.stopped,
                "still_running": pm._inproc_is_running(ROOM),
                "in_table": ROOM in pm._inproc_bots,
            }

        res = asyncio.run(scenario())
        print(f"  clean_room 前后: {res}")
        ok("清理前确实是「在运行」", res["before"] is True)
        ok("清理后不再算运行", res["still_running"] is False)
        ok("记账表里已移除", res["in_table"] is False)
        ok("**Bot Task 真的停了**（不是只忘掉记账）",
           res["task_done"] or res["task_cancelled"],
           f"done={res['task_done']} cancelled={res['task_cancelled']}")
        ok("**调用了 robot.shutdown()**", res["robot_shutdown"] is True)
        ok("调用了 auth.stop()", res["auth_stopped"] is True)

        print("[2] 目录是否真的能删掉（Windows 上文件被占用会失败）")
        # 上面 scenario 结束时 store 连接由 GC/进程退出处理；这里单独验证
        # 「Bot 没停就 rmtree」在 Windows 上会抛什么
        probe = tmp / "rooms" / "999000111"
        (probe / "data").mkdir(parents=True, exist_ok=True)
        st2 = StatsStore(str(probe / "data" / "bot.db"))
        st2.record_gift_event(GiftEvent(
            ts=1770000000, month=StatsStore.month_of_ts(1770000000), uid=1,
            uname="a", event_type="SEND_GIFT", gift_name="辣条", gift_num=1,
            is_blind_box=False, blind_box_cost=0, actual_value=100,
            profit_value=0, raw_json='{"total_coin":100}'))
        err = ""
        try:
            shutil.rmtree(str(probe))
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        st2._conn.close()
        print(f"  连接未关闭时 rmtree: {err or '成功（POSIX 允许删除打开的文件）'}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("ROOM DELETE RUNNING OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
