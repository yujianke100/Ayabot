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
import sys
import threading
import time
import webbrowser
from pathlib import Path

# ── 解析参数 ──
parser = argparse.ArgumentParser(description="Ayabot Windows Tray App")
parser.add_argument("--port", type=int, default=None, help="WebUI port")
parser.add_argument("--room", type=str, default=None, help="Room ID")
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

# ── 单实例锁（防止双击导致无限进程）─
_LOCK_FILE = DATA_DIR / "tray.lock"
_MUTEX_NAME = "AyabotTrayApp"


def _acquire_lock() -> bool:
    """尝试获取互斥锁。返回 True 表示本实例是第一个。"""
    # Windows: 使用内核命名互斥体（最可靠）
    if sys.platform == "win32":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
            if ctypes.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                ctypes.windll.kernel32.CloseHandle(handle)
                logger.warning("another instance already running (mutex)")
                return False
            _acquire_lock._mutex_handle = handle
            return True
        except Exception as exc:
            logger.warning("mutex failed, fallback to file lock: %s", exc)

    # Linux / macOS / fallback: 文件锁
    try:
        _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            old_pid = int(_LOCK_FILE.read_text(encoding="utf-8").strip())
            try:
                os.kill(old_pid, 0)
                logger.warning("another instance already running (PID=%d)", old_pid)
                return False
            except (ProcessLookupError, OSError):
                pass
        except (ValueError, OSError):
            pass
        _LOCK_FILE.unlink(missing_ok=True)
        return _acquire_lock()
    except OSError as exc:
        logger.warning("lock acquire failed: %s", exc)
        return True  # 非致命


def _release_lock() -> None:
    _LOCK_FILE.unlink(missing_ok=True)
    if sys.platform == "win32":
        try:
            handle = getattr(_acquire_lock, "_mutex_handle", None)
            if handle:
                import ctypes
                ctypes.windll.kernel32.ReleaseMutex(handle)
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


# ── 全局状态 ──
_port = _read_port()
_stop_event = threading.Event()

# ── In-process uvicorn ──


def _start_webui() -> None:
    """在后台线程启动 WebUI（in-process，兼容 PyInstaller）。
    使用 queue 确认线程内启动成功，否则日志记录具体异常。
    """
    import queue as _queue
    import uvicorn
    from app.web.server import app

    logger.info("starting WebUI in-process: port=%s", _port)
    q: _queue.Queue = _queue.Queue()

    def _run() -> None:
        try:
            config = uvicorn.Config(
                app,
                host="0.0.0.0",
                port=_port,
                log_level="info",
            )
            server = uvicorn.Server(config)
            _start_webui._server = server
            q.put("ready")
            server.run()
        except Exception as exc:
            logger.error("uvicorn thread crashed", exc_info=True)
            q.put(exc)

    t = threading.Thread(target=_run, daemon=True, name="uvicorn")
    t.start()

    try:
        status = q.get(timeout=15)
        if isinstance(status, Exception):
            logger.error("WebUI failed to start: %s", status)
            return
        logger.info("WebUI started (in-process)")
    except _queue.Empty:
        logger.error("WebUI start timed out after 15s — check log for details")
        return


def _stop_webui() -> None:
    server = getattr(_start_webui, "_server", None)
    if server:
        logger.info("stopping WebUI...")
        server.should_exit = True
        logger.info("WebUI stop signaled")


def _restart_webui() -> None:
    _stop_webui()
    time.sleep(1)
    _start_webui()


def _open_webui() -> None:
    webbrowser.open(f"http://127.0.0.1:{_port}")


def _reset_admin_password() -> None:
    """直接调用 reset_admin 模块重置密码（兼容 PyInstaller）。"""
    try:
        import io
        from contextlib import redirect_stdout

        from app.reset_admin import reset_admin as do_reset

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            do_reset(username="ayabot")
        msg = stdout_buf.getvalue().strip() or "密码已重置"
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
    if args.room:
        from app.config import ensure_room_dirs, migrate_legacy_data

        room_dir = ensure_room_dirs(
            args.room,
            base_dir=BASE_DIR if getattr(sys, "frozen", False) else ".",
        )
        target_cfg = room_dir / "config.yaml"

        if not target_cfg.exists():
            src = BASE_DIR / "config.yaml"
            if src.exists():
                import shutil
                shutil.copy2(str(src), str(target_cfg))
                logger.info("copied default config to %s", target_cfg)
            else:
                logger.warning("no config.yaml found at %s", src)

        migrate_legacy_data(
            args.room,
            base_dir=BASE_DIR if getattr(sys, "frozen", False) else ".",
        )
        os.chdir(room_dir)
        return str(target_cfg)
    else:
        cfg_path = DATA_DIR / "config.yaml"
        if not cfg_path.exists():
            src = BASE_DIR / "config.yaml"
            if src.exists():
                import shutil
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

    # 生成图标 — 按优先级搜索多个位置和文件名
    icon_size = 64
    icon_img = Image.new("RGBA", (icon_size, icon_size), (0, 0, 0, 0))

    icon_candidates = []
    # 1) PyInstaller 打包目录 (_MEIPASS)
    if getattr(sys, "_MEIPASS", None):
        meipass = Path(sys._MEIPASS)
        icon_candidates.extend([
            meipass / "icon.png",
            meipass / "logo.png",
        ])
    # 2) .exe 同级目录
    icon_candidates += [
        BASE_DIR / "icon.png",
        BASE_DIR / "logo.png",
        BASE_DIR / "assets" / "icon.png",
        BASE_DIR / "assets" / "logo.png",
    ]
    loaded = False
    for lp in icon_candidates:
        if lp.exists():
            try:
                icon_img = Image.open(lp).resize((icon_size, icon_size))
                logger.info("loaded icon from %s", lp)
                loaded = True
                break
            except Exception as exc:
                logger.debug("failed to load %s: %s", lp, exc)
                continue
    if not loaded:
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
        _release_lock()
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
    # 单实例检查
    if not _acquire_lock():
        logger.warning("another instance already running, exiting")
        return

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
    """延迟几秒后打开浏览器，最多等 30 秒."""
    time.sleep(2)
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", _port), timeout=2):
                _open_webui()
                logger.info("browser opened")
                return
        except (ConnectionRefusedError, OSError):
            time.sleep(1)


if __name__ == "__main__":
    main()
