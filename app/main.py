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
    ensure_room_dirs,
    load_config,
    migrate_legacy_data,
)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def _run(config_path: str) -> None:
    # 按需导入（避免 init 操作时触发 bilibili_api 等依赖）
    import uvicorn  # noqa: PLC0415
    from .auth import AuthManager  # noqa: PLC0415
    from .bot import LiveRobot  # noqa: PLC0415
    from .web.server import app as fastapi_app, init_app, _HTTP_HOST, _HTTP_PORT  # noqa: PLC0415

    config = load_config(config_path)
    _setup_logging(config.runtime.log_level)

    init_app(config, config_path=config_path)

    auth = AuthManager(config)
    credential = await auth.prepare_credential()
    auth.start_refresh_loop(credential)

    robot = LiveRobot(config=config, credential=credential)

    async def _run_web() -> None:
        cfg = uvicorn.Config(fastapi_app, host=_HTTP_HOST, port=_HTTP_PORT, log_level="info")
        server = uvicorn.Server(cfg)
        await server.serve()

    web_task = asyncio.create_task(_run_web())
    try:
        await robot.run()
    finally:
        web_task.cancel()
        try:
            await web_task
        except asyncio.CancelledError:
            pass
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

    try:
        asyncio.run(_run(config_path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
