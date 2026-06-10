from __future__ import annotations

import asyncio
import logging

import uvicorn

from .auth import AuthManager
from .bot import LiveRobot
from .config import load_config
from .web.server import app as fastapi_app


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def _run_web() -> None:
    """Run FastAPI web UI as an asyncio task alongside the bot."""
    cfg = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(cfg)
    await server.serve()


async def _run() -> None:
    config = load_config("config.yaml")
    _setup_logging(config.runtime.log_level)

    auth = AuthManager(config)
    credential = await auth.prepare_credential()
    auth.start_refresh_loop(credential)

    robot = LiveRobot(config=config, credential=credential)
    # 启动 Web UI 作为异步任务
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


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
