"""
Ayabot Windows Tray App
──────────────────────
Entry point for PyInstaller-packaged .exe.
- Runs the bot + Web UI in background
- System tray icon with right-click menu
- Opens browser on startup
- Config/data stored in %USERPROFILE%\.ayabot\

Dependencies: pystray, Pillow
  pip install pystray pillow
"""

import asyncio
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

# ── 检测 PyInstaller 打包环境 ──
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Ayabot"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    DATA_DIR = BASE_DIR / "data"

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


def _patch_config_path() -> None:
    """将 config.yaml 的默认路径改为 %USERPROFILE%\.ayabot\config.yaml"""
    cfg_path = DATA_DIR / "config.yaml"
    if not cfg_path.exists():
        src = BASE_DIR / "config.yaml"
        if src.exists():
            import shutil
            shutil.copy2(src, cfg_path)
            logger.info("copied default config to %s", cfg_path)
        else:
            logger.warning("no default config.yaml found at %s", src)
    # 切换工作目录到 DATA_DIR，让所有相对路径指向用户目录
    os.chdir(DATA_DIR)


def _start_bot() -> asyncio.AbstractEventLoop:
    """在独立线程中启动 asyncio 事件循环 + bot."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    from app.main import _run  # noqa: PLC2701
    loop.create_task(_run())
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

    menu = pystray.Menu(
        pystray.MenuItem("打开管理后台", on_open, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", on_exit),
    )

    icon = pystray.Icon("ayabot", icon_img, "Ayabot 直播间机器人", menu)
    icon.run()


def main() -> None:
    _patch_config_path()
    logger.info("Ayabot starting… data dir: %s", DATA_DIR)

    stop_event = threading.Event()

    # 在后台线程启动 bot
    bot_thread = threading.Thread(target=_start_bot, daemon=True)
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
