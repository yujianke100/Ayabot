"""
Ayabot Windows Tray App
──────────────────────
Entry point for PyInstaller-packaged .exe.
- Runs WebUI (uvicorn) in background via subprocess
- System tray icon with right-click menu:
  - 打开管理后台 (Open WebUI)
  - 设置端口 (Set Port)
  - 重置管理员密码 (Reset Admin Password)
  - 退出 (Exit)

Dependencies: pystray, Pillow
  pip install pystray pillow

PyInstaller:
  python scripts/build_exe.py
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

# ── 解析参数 ──
parser = argparse.ArgumentParser(description="Ayabot Windows Tray App")
parser.add_argument("--port", type=int, default=None, help="WebUI port")
args, _ = parser.parse_known_args()

# ── 路径 ──
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Ayabot"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    DATA_DIR = BASE_DIR / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── 端口 ──
_PORT_FILE = DATA_DIR / "port.txt"
_DEFAULT_PORT = 19810


def _read_port() -> int:
    if args.port:
        return args.port
    if _PORT_FILE.exists():
        try:
            return int(_PORT_FILE.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pass
    return _DEFAULT_PORT


def _save_port(port: int) -> None:
    _PORT_FILE.write_text(str(port), encoding="utf-8")


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

# ── 全局状态 ──
_port = _read_port()
_webui_proc: subprocess.Popen | None = None
_stop_event = threading.Event()


def _start_webui() -> None:
    """在后台线程启动 WebUI."""
    global _webui_proc
    python = sys.executable

    cmd = [
        python, "-m", "uvicorn", "app.web.server:app",
        "--host", "0.0.0.0",
        "--port", str(_port),
    ]

    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        env["PYTHONPATH"] = str(BASE_DIR)

    env["AYABOT_PORT"] = str(_port)

    logger.info("starting WebUI: port=%s", _port)
    try:
        _webui_proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logger.info("WebUI started, PID=%d", _webui_proc.pid)
    except Exception as exc:
        logger.error("failed to start WebUI: %s", exc)


def _stop_webui() -> None:
    global _webui_proc
    if _webui_proc and _webui_proc.poll() is None:
        logger.info("stopping WebUI...")
        _webui_proc.terminate()
        try:
            _webui_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _webui_proc.kill()
            _webui_proc.wait()
        logger.info("WebUI stopped")
    _webui_proc = None


def _restart_webui() -> None:
    _stop_webui()
    time.sleep(1)
    _start_webui()


def _open_webui() -> None:
    webbrowser.open(f"http://127.0.0.1:{_port}")


def _reset_admin_password() -> None:
    """运行 reset_admin 重置密码，显示结果弹窗."""
    python = sys.executable
    cmd = [python, "-m", "app.reset_admin"]
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        env["PYTHONPATH"] = str(BASE_DIR)

    try:
        result = subprocess.run(
            cmd, cwd=str(BASE_DIR), env=env,
            capture_output=True, text=True, timeout=30,
        )
        msg = result.stdout.strip() or result.stderr.strip() or "密码已重置"
    except Exception as exc:
        msg = f"重置失败: {exc}"

    logger.info("reset admin password: %s", msg)
    _show_message("密码重置", msg)


def _show_message(title: str, message: str) -> None:
    """显示消息框."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, message)
        root.destroy()
    except Exception:
        logger.info("%s: %s", title, message)


def _prompt_port() -> None:
    """弹窗让用户输入端口号."""
    global _port
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        new_port = simpledialog.askinteger(
            "设置端口",
            f"当前端口: {_port}\n输入新的 WebUI 端口号:",
            initialvalue=_port,
            minvalue=1024,
            maxvalue=65535,
            parent=root,
        )
        root.destroy()

        if new_port and new_port != _port:
            _port = new_port
            _save_port(_port)
            _restart_webui()
            _show_message("端口已更改", f"WebUI 已切换到端口 {_port}")
    except Exception as exc:
        logger.error("port prompt failed: %s", exc)


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


def _create_tray_icon() -> None:
    """创建系统托盘图标."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        logger.error("pystray or Pillow not installed. Run: pip install pystray pillow")
        return

    # 生成图标
    icon_size = 64
    icon_img = Image.new("RGBA", (icon_size, icon_size), (0, 0, 0, 0))

    logo_paths = [
        BASE_DIR / "logo.png",
        BASE_DIR / "assets" / "logo.png",
    ]
    for lp in logo_paths:
        if lp.exists():
            try:
                icon_img = Image.open(lp).resize((icon_size, icon_size))
                break
            except Exception:
                continue
    else:
        # 无 logo 文件时生成一个字母 A 图标
        draw = ImageDraw.Draw(icon_img)
        draw.ellipse([0, 0, icon_size - 1, icon_size - 1], fill="#00A1D6")
        draw.text((16, 12), "A", fill="white")

    def on_open(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        _open_webui()

    def on_set_port(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        _prompt_port()

    def on_reset_pwd(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        _reset_admin_password()

    def on_exit(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        icon.stop()
        _stop_webui()
        _stop_event.set()
        os._exit(0)

    label = f"Ayabot 直播间机器人 (端口 {_port})"
    menu = pystray.Menu(
        pystray.MenuItem("🌐 打开管理后台", on_open, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🔌 设置端口", on_set_port),
        pystray.MenuItem("🔑 重置管理员密码", on_reset_pwd),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❌ 退出", on_exit),
    )

    icon = pystray.Icon("ayabot", icon_img, label, menu)
    icon.run()


def main() -> None:
    logger.info("Ayabot starting… data dir: %s", DATA_DIR)

    # 确保 config.yaml 存在
    cfg_src = BASE_DIR / "config.yaml"
    cfg_dst = DATA_DIR / "config.yaml"
    if not cfg_dst.exists() and cfg_src.exists():
        import shutil
        shutil.copy2(str(cfg_src), str(cfg_dst))
        logger.info("copied config to %s", cfg_dst)

    # 启动 WebUI
    _start_webui()

    # 延迟打开浏览器
    threading.Thread(target=_delayed_open_browser, daemon=True).start()

    # 主线程：显示托盘图标
    logger.info("tray icon starting…")
    _create_tray_icon()


def _delayed_open_browser() -> None:
    """延迟 3 秒后打开浏览器."""
    time.sleep(3)
    for _ in range(10):
        try:
            with socket.create_connection(("127.0.0.1", _port), timeout=1):
                _open_webui()
                return
        except (ConnectionRefusedError, OSError):
            time.sleep(1)


if __name__ == "__main__":
    main()
