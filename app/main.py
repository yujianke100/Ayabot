from __future__ import annotations

import asyncio
import logging
from multiprocessing import Process
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

def run_web_ui() -> None:
    # 辅助进程运行 Web 管理界面
    uvicorn.run(fastapi_app, host="0.0.0.0", port=8000, log_level="error")

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
    # 启动 Web UI 进程
    web_process = Process(target=run_web_ui, daemon=True)
    web_process.start()
    
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        if web_process.is_alive():
            web_process.terminate()

if __name__ == "__main__":
    main()
