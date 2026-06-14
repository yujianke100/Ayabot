"""Ayabot WebUI — 独立管理面板入口."""
import argparse
import os
from app.web.server import app, init_app
import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ayabot WebUI")
    parser.add_argument("--host", type=str, default=None, help="监听地址")
    parser.add_argument("--port", type=int, default=None, help="监听端口")
    args = parser.parse_args()

    init_app()
    port = args.port or int(os.environ.get("AYABOT_PORT", "19810"))
    host = args.host or "0.0.0.0"
    uvicorn.run(app, host=host, port=port, log_level="info")
