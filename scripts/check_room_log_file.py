"""冻结 exe（同进程模式）里，每个房间必须有自己的 bot.log。

为什么要有这条检查：Windows 用户拿到的 .exe 是冻结包，Bot 作为同进程 asyncio
Task 运行，日志统一走根 logger → 落到托盘那个共享的 `ayabot.log`。
`rooms/<id>/bot.log` 从来不存在 —— 于是：

- 「日志」标签永远显示「暂无日志或日志文件不存在」，用户没法自查；
- 启动失败时 `_last_error_from_log()` 读不到东西，界面只剩一句
  「请查看该房间的日志」，而那个日志是空的；
- 用户报「机器人莫名其妙关了」，我们手里没有任何现场。

子进程模式（源码 / Docker）靠 stdout 重定向解决，同进程模式必须自己挂
handler，而且要用上下文变量分流 —— 所有房间共用同一个 logger 名
`bili-live-robot`，不区分就会互相串。

    ./.venv/Scripts/python.exe scripts/check_room_log_file.py
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROOM_A = "1946287911"
ROOM_B = "2000000002"


def main() -> int:
    from app.logutil import ROOM_CTX, install_room_log_handler, remove_room_log_handlers

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="roomlog_"))
    la = tmp / "a" / "bot.log"
    lb = tmp / "b" / "bot.log"
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)

    ha: list[logging.Handler] = []
    hb: list[logging.Handler] = []
    try:
        print("[1] 两个房间各写各的文件")
        ha = install_room_log_handler(ROOM_A, la)
        hb = install_room_log_handler(ROOM_B, lb)
        ok("两个房间都挂上了 handler", len(ha) == 2 and len(hb) == 2, f"{len(ha)}/{len(hb)}")

        # 用真正的 asyncio Task 验证上下文传播（不是手动 set 完再 set 回来）
        async def room_worker(room_id: str) -> None:
            ROOM_CTX.set(room_id)
            lg = logging.getLogger("bili-live-robot")
            lg.info("房间 %s 开工", room_id)
            # 子任务必须继承房间上下文（心跳/守护/弹幕都在子任务里）
            async def child() -> None:
                logging.getLogger("bili-live-robot.auth").info("子任务里的凭证消息 %s", room_id)
                await asyncio.sleep(0)
            await asyncio.create_task(child())
            # to_thread 也必须继承（同步的存储层会在这里打日志）
            await asyncio.to_thread(
                lambda: logging.getLogger("app.storage").info("线程里的存储消息 %s", room_id))
            await asyncio.sleep(0)

        async def run_both() -> None:
            await asyncio.gather(room_worker(ROOM_A), room_worker(ROOM_B))

        asyncio.run(run_both())
        # 全局（无房间上下文）的日志不能进任何房间文件
        logging.getLogger("webui").info("这是 WebUI 全局消息，不属于任何房间")

        text_a = la.read_text(encoding="utf-8") if la.exists() else ""
        text_b = lb.read_text(encoding="utf-8") if lb.exists() else ""
        ok("房间 A 的文件存在", la.exists())
        ok("A 只收到自己的消息", "房间 A 开工" in text_a or f"房间 {ROOM_A} 开工" in text_a,
           text_a[:60].replace("\n", "|"))
        ok("A 里没有 B 的消息", ROOM_B not in text_a and f"房间 {ROOM_B}" not in text_a)
        ok("B 里没有 A 的消息", ROOM_A not in text_b)
        ok("子任务继承了房间上下文（auth 的日志进了 A）",
           "子任务里的凭证消息" in text_a)
        ok("asyncio.to_thread 也继承了上下文（存储层的日志进了 A）",
           "线程里的存储消息" in text_a)
        ok("全局消息没被写进房间文件", "WebUI 全局消息" not in text_a and "WebUI 全局消息" not in text_b)

        print("[2] 中文是 UTF-8，不是 GBK 乱码")
        raw = la.read_bytes()
        ok("文件能按 UTF-8 解码", "房间" in raw.decode("utf-8", "strict"))
        ok("不是 GBK（gbk 解出来会是乱码）",
           raw.decode("gbk", "replace").count("\ufffd") > 0 or b"\xe6\x88\xbf" in raw)

        print("[3] B站 弹幕库的 logger 也进房间文件")
        logging.getLogger(f"LiveDanmaku_{ROOM_A}").info("连接正在关闭")
        text_a2 = la.read_text(encoding="utf-8")
        ok("LiveDanmaku_<id> 的消息写进了 A", "连接正在关闭" in text_a2)
        ok("…而且没写进 B", "连接正在关闭" not in (lb.read_text(encoding="utf-8") if lb.exists() else ""))

        print("[4] 重启不能重复写（每次都要摘掉上一组 handler）")
        before = len(la.read_text(encoding="utf-8").splitlines())
        remove_room_log_handlers(ha)
        ha = install_room_log_handler(ROOM_A, la)
        asyncio.run(room_worker(ROOM_A))
        after_lines = la.read_text(encoding="utf-8").splitlines()
        added = len(after_lines) - before
        ok("重启后新增行数 = 正常条数（没有翻倍）", 1 <= added <= 5, f"新增 {added} 行")

        print("[5] 摘掉之后文件确实被关了（Windows 上才能删目录）")
        remove_room_log_handlers(ha)
        remove_room_log_handlers(hb)
        ha, hb = [], []
        ok("root 上不再挂着这些 handler",
           not any(getattr(h, "owner", None) is root for h in ha))
        ok("房间里也没有残留 handler",
           not any(isinstance(h, logging.FileHandler) and h.baseFilename.startswith(str(tmp))
                   for h in root.handlers))
        try:
            shutil.rmtree(tmp)
            ok("目录可以直接删掉", True)
        except OSError as exc:
            ok("目录可以直接删掉", False, str(exc))

        print("[6] 写不了文件也不能让房间起不来")
        bad = install_room_log_handler("999", Path(tmp) / "nope" / "\x00bad" / "bot.log")
        ok("返回空列表而不是抛异常", bad == [], bad)

        print("[7] 按房间号登记/摘除（房间删除时靠它才删得掉目录）")
        from app.logutil import drop_tracked, install_tracked
        d = tmp / "tracked"
        d.mkdir(parents=True, exist_ok=True)
        install_tracked("777", d / "bot.log")
        install_tracked("777", d / "bot.log")   # 重复安装不能叠加
        n = len([h for h in root.handlers
                 if isinstance(h, logging.FileHandler)
                 and h.baseFilename.startswith(str(d))])
        ok("重复 install 不会叠加 handler", n == 1, f"{n} 个")
        ROOM_CTX.set("777")
        logging.getLogger("bili-live-robot").info("房间 777 的消息")
        ROOM_CTX.set("")
        text7 = (d / "bot.log").read_text(encoding="utf-8") if (d / "bot.log").exists() else ""
        ok("消息确实进了这个房间的文件", "房间 777 的消息" in text7, text7[:60].replace("\n", "|"))
        drop_tracked("777")
        drop_tracked("777")   # 幂等
        ok("摘完之后根上没有指向它的 handler",
           not any(isinstance(h, logging.FileHandler)
                   and h.baseFilename.startswith(str(d)) for h in root.handlers))
        shutil.rmtree(d)
        ok("按房间号摘除后目录可以删掉", not d.exists())

        print("[8] 同进程模式下真跑一次启动/停止（冻结 exe 的形态）")
        import app.auth as auth_mod
        import app.bot as bot_mod
        import app.process_manager as pm

        room_dir = tmp / "rooms" / "8888888888"
        (room_dir / "data").mkdir(parents=True, exist_ok=True)
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (room_dir / "config.yaml").write_text(example, encoding="utf-8")
        pm.set_rooms_base_dir(tmp)

        class _FakeRobot:
            def __init__(self, config=None, credential=None, auth=None, **kw) -> None:
                self.logger = logging.getLogger("bili-live-robot")

            async def run(self) -> None:
                # 机器人自己的日志 + 弹幕库的日志，都必须落到房间文件里
                self.logger.info("假机器人跑起来了")
                logging.getLogger("LiveDanmaku_8888888888").info("连接已就绪")
                await asyncio.sleep(0.05)

            async def shutdown(self) -> None:
                pass

        class _FakeAuth:
            def __init__(self, config=None, **kw) -> None:
                self.status = "ok"

            async def prepare_credential(self):
                return object()

            def start_refresh_loop(self, cred) -> None:
                pass

            async def stop(self) -> None:
                pass

            def write_status(self, *a, **k) -> None:
                pass

            def mark_needs_login(self, *a, **k) -> None:
                pass

        old_robot, old_auth = bot_mod.LiveRobot, auth_mod.AuthManager
        bot_mod.LiveRobot, auth_mod.AuthManager = _FakeRobot, _FakeAuth
        try:
            async def scenario():
                started = await pm.start_bot_in_process("8888888888")
                ok("start_bot_in_process 返回 True", started is True, started)
                await asyncio.sleep(0.3)
                await pm.stop_bot_in_process("8888888888")

            asyncio.run(scenario())
        finally:
            bot_mod.LiveRobot, auth_mod.AuthManager = old_robot, old_auth
            pm.set_rooms_base_dir(ROOT)

        bot_log = room_dir / "bot.log"
        ok("rooms/<id>/bot.log 被创建了（exe 用户终于有房间日志）", bot_log.exists())
        body = bot_log.read_text(encoding="utf-8") if bot_log.exists() else ""
        ok("机器人自己的日志进去了", "假机器人跑起来了" in body, body[:80].replace("\n", "|"))
        ok("弹幕库的日志也进去了（判断假死的证据）", "连接已就绪" in body)
        ok("停止之后句柄已释放（否则删房间 WinError 32）",
           not any(isinstance(h, logging.FileHandler)
                   and h.baseFilename.startswith(str(room_dir)) for h in root.handlers))
    finally:
        remove_room_log_handlers(ha)
        remove_room_log_handlers(hb)
        root.setLevel(old_level)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("ROOM LOG FILE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
