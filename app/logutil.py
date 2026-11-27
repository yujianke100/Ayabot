"""按房间分流的日志文件（冻结 exe 的同进程模式下唯一能拿到房间日志的办法）。

背景：两种运行模式写日志的方式完全不同。

- **子进程模式**（`python -m app.main --room <id>`，源码运行 / Docker）：
  `process_manager.start_bot()` 把子进程的 stdout/stderr 重定向到
  `rooms/<id>/bot.log`，房间日志天然是分开的。
- **同进程模式**（PyInstaller 冻结的 .exe，也就是 Windows 用户的默认形态）：
  Bot 是同一个进程里的 asyncio Task，日志统一走根 logger，落到托盘那个
  `%LOCALAPPDATA%/Ayabot/ayabot.log` 里 —— **`rooms/<id>/bot.log` 从来不存在**。

于是 .exe 用户的「日志」标签永远是「暂无日志或日志文件不存在」，
`_last_error_from_log()` 也读不到东西（启动失败只剩一句「请查看该房间的日志」）。
用户报「机器人莫名其妙关了」时，我们手里没有任何现场。

这里用 `contextvars` 解决「同一个 logger 名被多个房间共用」的问题：
`asyncio.create_task` 会复制当前上下文，所以在房间 Task 开头设置一次
`ROOM_CTX`，它派生的所有子任务（弹幕、心跳、守护、`asyncio.to_thread`
里的同步日志）都带着正确的房间号，记录就能被分流到对应文件。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from pathlib import Path

#: 当前日志记录所属的房间。空串 = 与房间无关（WebUI 全局操作、托盘）。
ROOM_CTX: ContextVar[str] = ContextVar("ayabot_room", default="")

_FORMAT = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class _RoomFilter(logging.Filter):
    """只放行「当前上下文就是这个房间」的记录。

    挂在**根** logger 上：子进程模式下 `bili-live-robot`、`bili-live-robot.auth`、
    `webui` 都是它的子 logger，都能命中；而 WebUI 自己的请求处理跑在没有房间
    上下文的 Task 里（ROOM_CTX 为空），不会被写进某个房间的文件。
    """

    def __init__(self, room_id: str) -> None:
        super().__init__()
        self.room_id = str(room_id)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return ROOM_CTX.get() == self.room_id


class RoomLogHandler(logging.FileHandler):
    """带「我挂在哪个 logger 上」记录的 FileHandler，方便成对摘除。

    `logging.Logger.removeHandler` 要拿到原 logger 对象；重启房间时要精确摘掉
    上一次挂的那一组，否则第二次启动后每行日志写两遍、第三次三遍。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.owner: logging.Logger | None = None


def install_room_log_handler(room_id: str, log_path: Path,
                            level: int | None = None) -> list[logging.Handler]:
    """给这个房间挂上文件日志，返回之后需要摘掉的 handler 列表。

    返回列表而不是单个 handler：除了根 logger（带房间过滤），还要单独挂到
    B站的 `LiveDanmaku_<room_id>` logger 上 —— 那条链路上有「连接正在关闭 /
    连接已关闭」这类判断假死的**关键证据**，而它不走房间上下文。
    """
    room_id = str(room_id)
    log_path = Path(log_path)
    handlers: list[logging.Handler] = []
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)

        root_fh = RoomLogHandler(log_path, encoding="utf-8", delay=True)
        root_fh.setFormatter(_FORMAT)
        root_fh.addFilter(_RoomFilter(room_id))
        root_logger = logging.getLogger()
        root_fh.owner = root_logger
        if level is not None:
            # handler 级别的过滤在 logger 之后：这样「房间 A 要 DEBUG、房间 B 要
            # WARNING」能各按各的来，而根 logger 的级别由 install_tracked 抬到最宽松。
            root_fh.setLevel(level)
        root_logger.addHandler(root_fh)
        handlers.append(root_fh)

        dm_logger = logging.getLogger(f"LiveDanmaku_{room_id}")
        dm_fh = RoomLogHandler(log_path, encoding="utf-8", delay=True)
        dm_fh.setFormatter(_FORMAT)
        dm_fh.owner = dm_logger
        if level is not None:
            dm_fh.setLevel(level)
        dm_logger.addHandler(dm_fh)
        handlers.append(dm_fh)
    except (OSError, ValueError) as exc:
        # 磁盘满 / 只读目录 / 路径非法：日志写不了不能让房间起不来
        logging.getLogger("bili-live-robot").warning(
            "房间 %s 无法写日志文件 %s：%s", room_id, log_path, exc)
        remove_room_log_handlers(handlers)
        return []
    return handlers


#: room_id → 已挂上的 handler。房间停止/删除时必须按这张表摘干净：
#: Task 的 finally 在极端情况（事件循环被直接关掉）里可能来不及跑，
#: 而 Windows 上只要有一个 FileHandler 还开着，删除房间就会 WinError 32。
_INSTALLED: dict[str, list[logging.Handler]] = {}


def install_tracked(room_id: str, log_path: Path, level: int | None = None) -> None:
    """`install_room_log_handler` 的登记版本（同进程模式用这个）。

    同时把根 logger 的级别**下调**到最宽松的那个房间要求的级别：
    冻结 exe 里根 logger 是托盘设的 INFO，用户在「机器人配置」里改成 DEBUG
    以前完全不起作用（同进程模式从不读 `config.runtime.log_level`）。
    这里只降不升 —— 绝不因为某个房间要 WARNING 就把别的房间的日志吞掉。
    """
    drop_tracked(room_id)          # 先摘上一次的，避免重启后重复写
    handlers = install_room_log_handler(room_id, log_path, level=level)
    if not handlers:
        return
    _INSTALLED[str(room_id)] = handlers
    if level is not None:
        root = logging.getLogger()
        current = root.level if root.level != logging.NOTSET else logging.WARNING
        if level < current:
            root.setLevel(level)


def drop_tracked(room_id: str) -> None:
    """按房间号摘掉并关闭 handler（幂等）。"""
    remove_room_log_handlers(_INSTALLED.pop(str(room_id), []))


def remove_room_log_handlers(handlers: list[logging.Handler]) -> None:
    """摘掉并关闭 handler。

    **必须**在房间停止时调用：同进程模式下重启一次就会再挂一组，
    不摘的话第二次启动后每行日志写两遍、第三次三遍 —— 日志翻倍增长，
    用户会以为「机器人一直在崩」。
    """
    for fh in handlers or []:
        owner = getattr(fh, "owner", None)
        if isinstance(owner, logging.Logger):
            try:
                owner.removeHandler(fh)
            except Exception:  # noqa: BLE001
                pass
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass
