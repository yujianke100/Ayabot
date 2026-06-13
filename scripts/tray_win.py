"""
Ayabot Windows Tray App
──────────────────────
Entry point for PyInstaller-packaged .exe.
- Runs the bot + Web UI in background
- System tray icon with right-click menu
- Opens browser on startup
- Per-room data isolation: %USERPROFILE%\\.ayabot\\rooms\\<room_id>\\

Usage:
    python scripts/tray_win.py                    # 默认加载 config.yaml
    python scripts/tray_win.py --room 12345       # 加载 rooms/12345/config.yaml

Dependencies: pystray, Pillow
  pip install pystray pillow
"""

import argparse
import asyncio
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

# ── 解析参数 ──
parser = argparse.ArgumentParser(description="Ayabot Windows Tray App")
parser.add_argument("--room", type=str, default=None,
                    help="Room ID for per-room data isolation")
args, _ = parser.parse_known_args()
ROOM_ID = args.room

# ── 路径 ──
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
    APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Ayabot"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    APP_DATA = BASE_DIR / "data"

if ROOM_ID:
    DATA_DIR = APP_DATA / "rooms" / ROOM_ID
else:
    DATA_DIR = APP_DATA

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── 日志 ──
log_path = DATA_DIR / "ayabot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("tray")


def _setup_room() -> str | None:
    """初始化房间: 复制 config.yaml + 迁移旧数据，返回 config_path."""
    if ROOM_ID:
        from app.config import ensure_room_dirs, migrate_legacy_data  # noqa: PLC0415

        # 确保房间目录存在
        room_dir = ensure_room_dirs(ROOM_ID, base_dir=BASE_DIR if getattr(sys, "frozen", False) else ".")
        target_cfg = room_dir / "config.yaml"

        # 如果房间没有 config.yaml，从 .exe 同级复制
        if not target_cfg.exists():
            src = BASE_DIR / "config.yaml"
            if src.exists():
                import shutil  # noqa: PLC0415
                shutil.copy2(str(src), str(target_cfg))
                logger.info("copied default config to %s", target_cfg)
            else:
                logger.warning("no config.yaml found at %s", src)

        # 迁移旧 data/ 到房间目录
        migrate_legacy_data(ROOM_ID, base_dir=BASE_DIR if getattr(sys, "frozen", False) else ".")

        os.chdir(room_dir)
        return str(target_cfg)
    else:
        # 传统模式: config.yaml 在 DATA_DIR
        cfg_path = DATA_DIR / "config.yaml"
        if not cfg_path.exists():
            src = BASE_DIR / "config.yaml"
            if src.exists():
                import shutil  # noqa: PLC0415
                shutil.copy2(str(src), str(cfg_path))
                logger.info("copied default config to %s", cfg_path)
            else:
                logger.warning("no config.yaml found at %s", src)

        os.chdir(DATA_DIR)
        return str(cfg_path)


def _start_bot(config_path: str) -> asyncio.AbstractEventLoop:
    """在独立线程中启动 asyncio 事件循环 + bot."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    from app.web.server import app as fastapi_app, init_app, _HTTP_HOST, _HTTP_PORT  # noqa: PLC0415, PLC2701
    from app.main import _run  # noqa: PLC0415, PLC2701

    loop.create_task(_run(config_path))
    loop.run_forever()
    return loop


def _open_webui() -> None:
    """在浏览器中打开 Web UI."""
    webbrowser.open(f"http://127.0.0.1:8000")


def _create_tray_icon(stop_event: threading.Event) -> None:
    """创建系统托盘图标."""
    try:
        import pystray
        from PIL import Image
    except ImportError:
        logger.error(
            "pystray or Pillow not installed. Run: pip install pystray pillow"
        )
        return

    # 生成图标（或从文件加载）
    icon_size = 64
    icon_img = Image.new("RGBA", (icon_size, icon_size), (0, 0, 0, 0))
    # 尝试加载 logo
    logo_paths = [
        BASE_DIR / "logo.png",
        BASE_DIR / "assets" / "logo.png",
        BASE_DIR / "app" / "web" / "static" / "logo.png",
        DATA_DIR / "logo.png",
    ]
    for lp in logo_paths:
        if lp.exists():
            try:
                icon_img = Image.open(lp).resize((icon_size, icon_size))
                logger.info("loaded icon from %s", lp)
                break
            except Exception:
                continue
    else:
        # 如果没有 logo 文件，生成一个简单的 A 字母图标
        try:
            from PIL import ImageDraw
            draw = ImageDraw.Draw(icon_img)
            draw.ellipse([0, 0, icon_size - 1, icon_size - 1], fill="#00A1D6")
            draw.text((16, 12), "A", fill="white", font=None)
        except Exception:
            pass

    def on_open(icon: pystray.Icon, item: pystray.MenuItem) -> None:  # noqa: ARG001
        _open_webui()

    def on_exit(icon: pystray.Icon, item: pystray.MenuItem) -> None:  # noqa: ARG001
        icon.stop()
        stop_event.set()
        # 强制退出（asyncio 循环在另一个线程）
        os._exit(0)

    label = f"Ayabot{' [' + ROOM_ID + ']' if ROOM_ID else ''} 直播间机器人"
    menu = pystray.Menu(
        pystray.MenuItem("打开管理后台", on_open, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", on_exit),
    )

    icon = pystray.Icon("ayabot", icon_img, label, menu)
    icon.run()


def main() -> None:
    config_path = _setup_room()
    logger.info("Ayabot starting… data dir: %s", DATA_DIR)
    if ROOM_ID:
        logger.info("room mode: %s, config: %s", ROOM_ID, config_path)

    stop_event = threading.Event()

    # 在后台线程启动 bot
    bot_thread = threading.Thread(target=_start_bot, args=(config_path,), daemon=True)
    bot_thread.start()

    # 短延迟后打开浏览器（等待 Web UI 就绪）
    def _delayed_open() -> None:
        import time
        time.sleep(3)
        _open_webui()

    threading.Thread(target=_delayed_open, daemon=True).start()

    # 主线程：显示托盘图标
    logger.info("tray icon starting…")
    _create_tray_icon(stop_event)


if __name__ == "__main__":
    main()
