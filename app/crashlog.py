"""进程级崩溃现场：线程未捕获异常 + faulthandler 硬崩溃转储。

**为什么要单独一个模块**：两种运行形态的入口不一样。

- 源码 / Docker：`python -m app.main --room <id>`，一个房间一个进程，
  入口是 `app/main.py`；
- Windows 安装版（`Ayabot.exe`）：入口是 `scripts/tray_win.py`，WebUI 和
  **所有房间机器人都在同一个进程里**（冻结模式走 `start_bot_in_process`），
  它**从不经过 `app/main.py`**。

钩子原来只装在 `app/main.py` 里，于是最常报「机器人莫名其妙就没了」的那批用户
（Windows exe）恰恰一点现场都没有。这里把钩子抽出来，两个入口都调。

采集什么：

- `sys.excepthook` / `threading.excepthook` —— 非主线程（托盘、心跳、守护、
  各房间 Task 的同步线程）抛异常时默认只写 stderr；窗口模式（console=False）
  下 stderr 已经被接到 devnull，线程悄悄死掉、进程还活着。
- `faulthandler` —— C 扩展访问违例、栈溢出这类硬崩溃连异常钩子都走不到，
  只能靠它留下最后一帧。
"""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import sys
import threading
from pathlib import Path

#: faulthandler 的文件句柄：不持有引用会被 GC 关掉，硬崩溃时就写不出东西了
_CRASH_FP: list = []

#: 单个崩溃转储的上限。硬崩溃可能被反复触发（比如每次启动都在同一处炸），
#: 不截断的话 crash.log 能长到几百 MB。
MAX_CRASH_LOG_BYTES = 1_000_000


def install_loop_exception_handler(loop=None) -> bool:
    """把事件循环的异常处理器指到日志上（返回是否装上）。

    **为什么不能只靠 asyncio 默认**：默认处理器把「Task exception was never
    retrieved」写到 stderr，而窗口模式（console=False）的 exe 已经把
    stdout/stderr 接到 devnull —— 一个 fire-and-forget 任务炸掉，用户在日志里
    一个字也看不到，现象就是「机器人某块功能突然不动了」。

    同进程模式下所有房间机器人 + WebUI 共用**同一个**循环，所以要装在
    uvicorn 起的那一个上（`init_app` 里装，两个入口都会经过）。
    """
    try:
        target = loop
        if target is None:
            target = asyncio.get_running_loop()
    except RuntimeError:
        return False
    logger = logging.getLogger("bili-live-robot")

    def _handler(loop_obj, ctx):  # noqa: ANN001
        message = ctx.get("message") or "asyncio error"
        exc = ctx.get("exception")
        task = ctx.get("task") or ctx.get("future")
        name = getattr(task, "get_name", lambda: "")() if task is not None else ""
        logger.error("asyncio loop error: %s%s", message,
                     f" (task={name})" if name else "", exc_info=exc)

    try:
        target.set_exception_handler(_handler)
        return True
    except Exception:  # noqa: BLE001
        return False


def install_crash_hooks(data_dir: Path | str | None = None) -> None:
    """装上进程级崩溃钩子；给了 `data_dir` 就同时开 faulthandler 写 crash.log。

    幂等：重复调用只会再挂一个新的 faulthandler 文件句柄（同目录下覆盖写同一个
    文件是安全的，faulthandler 以追加方式写）。
    """
    logger = logging.getLogger("bili-live-robot")

    def _excepthook(exc_type, exc, tb):  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("unhandled exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook

    def _thread_hook(args):  # noqa: ANN001
        if args.exc_type is SystemExit:
            return
        logger.critical(
            "thread %s crashed", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook

    if data_dir is None:
        return
    try:
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        crash_log = data_dir / "crash.log"
        # 别让一份写了几百次的崩溃转储无限长大
        try:
            if crash_log.exists() and crash_log.stat().st_size > MAX_CRASH_LOG_BYTES:
                crash_log.write_text("", encoding="utf-8")
        except OSError:
            pass
        fp = open(crash_log, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        faulthandler.enable(file=fp, all_threads=True)
        _CRASH_FP.append(fp)
        logger.info("crash hooks installed, faulthandler -> %s", crash_log)
    except Exception:
        # 拿不到崩溃转储不能影响机器人本身
        logger.warning("faulthandler 启用失败", exc_info=True)
