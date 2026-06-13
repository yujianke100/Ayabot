"""Ayabot WebUI — 独立管理面板入口."""
from app.web.server import app, init_app
import uvicorn

if __name__ == "__main__":
    init_app()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
