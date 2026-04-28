from __future__ import annotations

import asyncio
import logging

from .auth import AuthManager
from .bot import LiveRobot
from .config import load_config


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def _run() -> None:
    config = load_config("config.yaml")
    _setup_logging(config.runtime.log_level)

    auth = AuthManager(config)
    credential = await auth.prepare_credential()
    auth.start_refresh_loop(credential)

    robot = LiveRobot(config=config, credential=credential)
    try:
        await robot.run()
    finally:
        await auth.stop()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
