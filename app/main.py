#!/usr/bin/env python3
"""
Ayabot — Bilibili Live Robot
─────────────────────────────
Usage:
    python -m app.main                    # 默认加载 config.yaml
    python -m app.main --room 12345       # 加载 rooms/12345/config.yaml
    python -m app.main --room init 12345  # 初始化房间目录（从根 config.yaml 复制）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from pathlib import Path

from .config import (
    ConfigError,
    ensure_room_dirs,
    load_config,
    migrate_legacy_data,
)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


#: faulthandler 的文件句柄（兼容旧名字，实际在 app/crashlog.py 里持有）
_CRASH_FP: list = []


def _install_exception_hooks(data_dir: Path | None = None) -> None:
    """把未捕获异常写进日志，避免进程静默退出（Windows 上尤其重要）。

    修的是用户那句「有时候机器人莫名其妙就没了，点一下重启又好了」：
      - 主线程有 sys.excepthook（本来就有）；
      - **非主线程没有** —— 托盘线程、各后台循环一旦抛异常，默认只写 stderr，
        而窗口模式（console=False）下 tray_win 已经把 stdout/stderr 接到 devnull，
        于是线程悄悄死掉、进程还活着，表现就是「图标还在但功能没了」；
      - **硬崩溃（C 扩展访问违例、栈溢出）连异常钩子都走不到**，进程直接消失，
        bot.log 里一个字都不会多。faulthandler 至少能留下最后一帧。

    实现搬到 `app/crashlog.py` 了：`Ayabot.exe`（托盘入口）根本不走这个模块，
    钩子必须两边都能装，否则最常报崩溃的那批用户永远没有现场。
    """
    from .crashlog import install_crash_hooks  # noqa: PLC0415
    install_crash_hooks(data_dir)


async def _run(config_path: str) -> None:
    # 按需导入（避免 init 操作时触发 bilibili_api 等依赖）
    from .auth import AuthManager, CredentialError  # noqa: PLC0415
    from .bot import LiveRobot  # noqa: PLC0415

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        # 子进程模式（非冻结 exe）：配置写错时不要甩 traceback。
        # bot.log 里应该只有一行人能看懂的话，否则守护进程会一遍遍重启，
        # 用户翻到的永远是「invalid literal for int()」这种没头没尾的栈。
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        logging.getLogger("bili-live-robot").error("%s", exc)
        return
    _setup_logging(config.runtime.log_level)
    from .crashlog import install_loop_exception_handler  # noqa: PLC0415
    install_loop_exception_handler()

    from app.web.server import init_app  # noqa: PLC0415
    init_app(config, config_path=config_path)

    auth = AuthManager(config)
    try:
        credential = await auth.prepare_credential()
    except CredentialError as exc:
        # 凭证不可用且未配置等待登录：给出可操作的提示，而不是抛栈
        auth.mark_needs_login(str(exc))
        logging.getLogger("bili-live-robot").error("%s", exc)
        return
    auth.start_refresh_loop(credential)

    robot = LiveRobot(config=config, credential=credential, auth=auth)
    try:
        await robot.run()
    finally:
        await auth.stop()


def _init_room(room_id: str) -> None:
    """初始化房间目录: 复制 config.yaml + 迁移旧数据"""
    room_dir = ensure_room_dirs(room_id)
    target = room_dir / "config.yaml"
    if target.exists():
        print(f"Room {room_id} already exists: {target}")
        return

    sources = [
        Path("config.yaml"),
        Path("config.example.yaml"),
    ]
    for src in sources:
        if src.exists():
            shutil.copy2(str(src), str(target))
            print(f"Created room {room_id}: copied {src} → {target}")
            print(f"  Edit {target} to set room_display_id, port, etc.")
            # 迁移旧数据
            migrate_legacy_data(room_id)
            return

    print("No config.yaml or config.example.yaml found in project root!")
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ayabot Bilibili Live Robot")
    parser.add_argument("--room", type=str, default=None,
                        help="Room ID to run (uses rooms/<room_id>/config.yaml)")
    parser.add_argument("action", nargs="?", default=None,
                        help="Action: 'init' to initialize a room directory")

    args, _ = parser.parse_known_args()

    if args.action == "init":
        if not args.room:
            print("Usage: python -m app.main --room <room_id> init")
            sys.exit(1)
        _init_room(args.room)
        return

    # ── 确定配置文件路径 ──
    if args.room:
        room_dir = ensure_room_dirs(args.room)
        config_path = str(room_dir / "config.yaml")
        if not Path(config_path).exists():
            print(f"Room config not found: {config_path}")
            print(f"  Run: python -m app.main --room {args.room} init")
            sys.exit(1)
    else:
        config_path = "config.yaml"

    # 崩溃转储放到房间自己的 data/ 下（和 bot.log 同目录，用户找得到）
    crash_dir = Path(config_path).resolve().parent / "data"
    try:
        _install_exception_hooks(crash_dir)
        asyncio.run(_run(config_path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
