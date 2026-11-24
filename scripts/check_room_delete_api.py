"""端到端：房间在跑（同进程模式）时调用 `DELETE /api/rooms/{id}`。

这是 Windows 用户最常见的路径（冻结 exe = 同进程 Bot）。要求：
  - 接口返回 200
  - 房间目录真的被删掉（不是删一半）
  - 那个 Bot Task 真的停了

    ./.venv/Scripts/python.exe scripts/check_room_delete_api.py
"""
from __future__ import annotations

import asyncio
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

PORT = 18979
ROOM = "1946287911"
TOKEN = "rm-api"


def main() -> int:
    import uvicorn
    from app import process_manager as pm
    from app.web import server as srv
    from app.storage import GiftEvent, StatsStore

    tmp = Path(tempfile.mkdtemp(prefix="rmapi_"))
    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    try:
        pm.set_rooms_base_dir(str(tmp))
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (tmp / "data").mkdir(parents=True)
        (tmp / "config.yaml").write_text(example, encoding="utf-8")
        room = tmp / "rooms" / ROOM
        (room / "data").mkdir(parents=True)
        (room / "config.yaml").write_text(example, encoding="utf-8")

        # 真开一个库连接并写数据：模拟 Bot 正在工作
        store = StatsStore(str(room / "data" / "bot.db"))
        store.record_gift_event(GiftEvent(
            ts=1770000000, month=StatsStore.month_of_ts(1770000000), uid=1, uname="a",
            event_type="SEND_GIFT", gift_name="辣条", gift_num=1, is_blind_box=False,
            blind_box_cost=0, actual_value=100, profit_value=0,
            raw_json='{"total_coin":100}'))

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._API_TOKEN = TOKEN
        srv._SESSIONS[TOKEN] = (time.time() + 3600, "t", "admin", [])
        srv._rate_limited = lambda ip: False  # type: ignore[assignment]

        cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="critical")
        server = uvicorn.Server(cfg)
        loop_holder: dict = {}

        def run() -> None:
            loop = asyncio.new_event_loop()
            loop_holder["loop"] = loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(server.serve())

        threading.Thread(target=run, daemon=True).start()
        for _ in range(150):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)

        # 在 WebUI 的事件循环里注册一个「正在跑」的同进程 Bot
        stop = asyncio.Event()

        async def fake_bot() -> None:
            while not stop.is_set():
                await asyncio.sleep(0.05)

        class FakeRobot:
            shut_down = False

            async def shutdown(self) -> None:
                FakeRobot.shut_down = True
                stop.set()

        class FakeAuth:
            stopped = False

            async def stop(self) -> None:
                self.stopped = True

        loop = loop_holder["loop"]
        fut = asyncio.run_coroutine_threadsafe(
            _register(pm, ROOM, fake_bot, FakeRobot(), FakeAuth()), loop)
        task, auth = fut.result(timeout=10)
        time.sleep(0.2)
        ok("注册前算「在运行」", pm._inproc_is_running(ROOM) is True)

        # 先把测试自己开的那条连接关掉：真实 Bot 的 shutdown() 会 close 自己的 store，
        # 这里如果留着，Windows 上是「测试占用文件」而不是产品问题。
        store.close()

        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/rooms/{ROOM}",
                                     method="DELETE")
        req.add_header("Cookie", f"session={TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                code, body = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            code, body = exc.code, exc.read().decode("utf-8", "replace")
        print(f"  DELETE /api/rooms/{ROOM} -> HTTP {code} {body[:90]}")
        ok("接口返回 200", code == 200, f"HTTP {code} {body[:80]}")
        ok("房间目录真的没了", not room.exists(), str(room))
        ok("Bot Task 停了", task.done() or task.cancelled(),
           f"done={task.done()} cancelled={task.cancelled()}")
        ok("robot.shutdown() 被调用", FakeRobot.shut_down is True)
        ok("auth.stop() 被调用", auth.stopped is True)

        server.should_exit = True
        time.sleep(0.3)
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("ROOM DELETE API OK")
    return 0


async def _register(pm, room_id, factory, robot, auth):
    task = asyncio.create_task(factory())
    pm._inproc_bots[room_id] = ({"robot": robot}, auth, task)
    return task, auth


if __name__ == "__main__":
    sys.exit(main())
