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
import asyncio
import json
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
    if sys.platform == "win32":
        data_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    elif sys.platform == "darwin":
        data_root = Path.home() / "Library" / "Application Support"
    else:
        data_root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    DATA_DIR = data_root / "Ayabot"
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


# ── 日志（自动轮转，最多保留 5000 行） ──
log_path = DATA_DIR / "ayabot.log"
_MAX_LOG_LINES = 5000


class _RotatingFileHandler(logging.Handler):
    """限制日志文件行数，超出时截断末尾。"""
    def __init__(self, path: Path, max_lines: int = _MAX_LOG_LINES) -> None:
        super().__init__()
        self._path = path
        self._max_lines = max_lines

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(self.format(record) + "\n")
            # 超出限制时截断（惰性，避免每次写入都检查）
            if self._path.stat().st_size > 512 * 1024:  # 约 5000 行
                self._truncate()
        except Exception:
            self.handleError(record)

    def _truncate(self) -> None:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
            if len(lines) > self._max_lines:
                tail = lines[-self._max_lines:]
                self._path.write_text("\n".join(tail) + "\n", encoding="utf-8")
        except Exception:
            pass


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        _RotatingFileHandler(log_path),
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


def _start_webui(config_path: str | None = None) -> None:
    """在后台线程启动 WebUI（in-process，兼容 PyInstaller）。
    必须先调用 init_app 初始化配置，再启动 uvicorn。
    """
    import queue as _queue
    import uvicorn
    from app.web.server import app, init_app

    # PyInstaller windowed 模式下 sys.stderr/stdout 为 None，uvicorn logging 会崩溃
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")

    # 关键: 初始化配置 (DB路径、认证信息、LLM配置等)
    try:
        from app.config import load_config
        cfg = load_config(config_path or "config.yaml")
        init_app(config=cfg, config_path=config_path or "config.yaml")
        logger.info("init_app done, config_path=%s", config_path)
    except Exception as exc:
        logger.error("init_app failed: %s", exc, exc_info=True)
        return

    logger.info("starting WebUI in-process: port=%s", _port)
    q: _queue.Queue = _queue.Queue()

    def _run() -> None:
        try:
            config = uvicorn.Config(
                app,
                host="0.0.0.0",
                port=_port,
                log_level="info",
                log_config=None,  # 避免 PyInstaller windowed 模式下 sys.stderr=None 导致崩溃
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
    """重置 ayabot 密码为 123456。"""
    try:
        import io
        from contextlib import redirect_stdout

        from app.reset_admin import reset_admin as do_reset

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            do_reset(username="ayabot", password="123456", must_reset=False)
        msg = stdout_buf.getvalue().strip() or "密码已重置为 123456"
    except Exception as exc:
        msg = f"重置失败: {exc}"

    logger.info("reset admin password: %s", msg)
    _show_message("密码重置", msg)


def _show_message(title: str, message: str) -> None:
    """用 PowerShell 原生消息框（完全独立进程，避免 PyInstaller --windowed 下的焦点问题）。"""
    import subprocess
    try:
        # 用双引号包裹，内部双引号转义
        safe_msg = message.replace('"', '""')
        safe_title = title.replace('"', '""')
        ps = f'Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show("{safe_msg}", "{safe_title}")'
        subprocess.run(
            ['powershell', '-NoProfile', '-WindowStyle', 'Normal', '-Command', ps],
            capture_output=True, timeout=60
        )
    except Exception as exc:
        logger.info("%s: %s (PowerShell failed: %s)", title, message, exc)


def _prompt_port() -> None:
    """弹窗让用户输入端口号（PowerShell InputBox，完全独立于 tkinter）。"""
    global _port
    import subprocess
    import re
    try:
        ps = f'''
Add-Type -AssemblyName Microsoft.VisualBasic
$port = [Microsoft.VisualBasic.Interaction]::InputBox("当前端口: {_port}`n输入新的 WebUI 端口号:", "设置端口", "{_port}")
if ($port) {{ Write-Host $port }}
'''
        result = subprocess.run(
            ['powershell', '-NoProfile', '-WindowStyle', 'Normal', '-Command', ps],
            capture_output=True, text=True, timeout=60
        )
        output = result.stdout.strip()
        if not output:
            logger.info("port prompt cancelled")
            return
        # 提取第一个数字
        match = re.search(r'\d+', output)
        if not match:
            return
        new_port = int(match.group())
        if new_port < 1024 or new_port > 65535:
            _show_message("无效端口", "端口必须在 1024-65535 之间")
            return
        if new_port == _port:
            return
        _port = new_port
        _save_port(_port)
        _restart_webui()
        _show_message("端口已更改", f"WebUI 已切换到端口 {_port}")
    except Exception as exc:
        logger.error("port prompt failed: %s", exc)
        _show_message("错误", f"端口设置失败: {exc}")


def _view_logs() -> None:
    """打开日志查看窗口（级别筛选 + 滚动显示）。"""
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        _show_message("错误", "Tkinter 不可用，无法打开日志窗口")
        return

    LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]

    win = tk.Tk()
    win.title("Ayabot 日志查看")
    win.geometry("800x500")
    win.minsize(400, 250)

    # 尝试设置图标
    try:
        ico = Path(sys._MEIPASS) / "icon.png" if getattr(sys, "_MEIPASS", None) else BASE_DIR / "icon.png"
        if ico.exists():
            from PIL import Image, ImageTk
            img = ImageTk.PhotoImage(Image.open(ico).resize((32, 32)))
            win.iconphoto(True, img)
    except Exception:
        pass

    # ── 顶部：级别选择 + 刷新按钮 ──
    top = tk.Frame(win)
    top.pack(fill=tk.X, padx=10, pady=(10, 5))

    tk.Label(top, text="日志级别:").pack(side=tk.LEFT, padx=(0, 5))

    level_var = tk.StringVar(value="INFO")
    level_combo = ttk.Combobox(top, textvariable=level_var, values=LOG_LEVELS, state="readonly", width=12)
    level_combo.pack(side=tk.LEFT, padx=(0, 10))

    level_order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}

    # ── 状态栏 ──
    status_var = tk.StringVar(value="")
    status_bar = tk.Label(win, textvariable=status_var, anchor=tk.W, fg="gray", font=("", 9))
    status_bar.pack(fill=tk.X, padx=10, pady=(0, 5))

    # ── 日志内容显示 ──
    text_frame = tk.Frame(win)
    text_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    text_widget = tk.Text(text_frame, wrap=tk.NONE, font=("Consolas", 10), bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
    scroll_y = tk.Scrollbar(text_frame, orient=tk.VERTICAL, command=text_widget.yview)
    scroll_x = tk.Scrollbar(text_frame, orient=tk.HORIZONTAL, command=text_widget.xview)
    text_widget.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)

    scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
    scroll_x.pack(side=tk.BOTTOM, fill=tk.X)
    text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # 用 tag 给不同级别上色
    text_widget.tag_configure("DEBUG", foreground="#569cd6")
    text_widget.tag_configure("INFO", foreground="#d4d4d4")
    text_widget.tag_configure("WARNING", foreground="#dcdcaa")
    text_widget.tag_configure("ERROR", foreground="#f44747")

    def _load_logs() -> None:
        """读取日志文件并按级别筛选显示。"""
        text_widget.delete("1.0", tk.END)
        selected = level_var.get()
        min_order = level_order.get(selected, 1)

        log_file = log_path
        if not log_file.exists():
            text_widget.insert(tk.END, f"日志文件不存在: {log_file}\n", "ERROR")
            status_var.set("日志文件不存在")
            return

        try:
            content = log_file.read_text(encoding="utf-8")
        except Exception as exc:
            text_widget.insert(tk.END, f"读取日志失败: {exc}\n", "ERROR")
            status_var.set("读取失败")
            return

        lines = content.splitlines()
        shown = 0
        for line in lines:
            # 解析行首的日志级别: "2026-06-15 14:54:59,578 [LEVEL]"
            level_tag = None
            for lvl in LOG_LEVELS:
                if f"[{lvl}]" in line:
                    level_tag = lvl
                    break
            if level_tag is None:
                # 没有级别的行（如 traceback），如果上一行级别够就显示
                if shown > 0:
                    text_widget.insert(tk.END, line + "\n", selected if selected in LOG_LEVELS else "INFO")
                continue

            tag = level_tag
            if level_order.get(level_tag, 1) >= min_order:
                text_widget.insert(tk.END, line + "\n", tag)
                shown += 1

        if shown == 0:
            text_widget.insert(tk.END, f"没有 {selected} 及以上级别的日志。\n", "DEBUG")
        status_var.set(f"共 {len(lines)} 行，显示 {shown} 行 [{selected}+]")
        # 自动滚动到底部
        text_widget.see(tk.END)

    # ── 刷新按钮 ──
    def _on_refresh() -> None:
        _load_logs()

    refresh_btn = tk.Button(top, text="🔄 刷新", command=_on_refresh)
    refresh_btn.pack(side=tk.LEFT, padx=(0, 10))

    # ── 自动刷新开关 ──
    auto_var = tk.BooleanVar(value=True)
    auto_cb = tk.Checkbutton(top, text="自动刷新", variable=auto_var)
    auto_cb.pack(side=tk.LEFT, padx=(0, 10))

    # ── 清空按钮 ──
    def _on_clear_log() -> None:
        from tkinter import messagebox
        if messagebox.askyesno("清空日志", "确定清空所有日志吗？\n（文件将被清空，不可恢复）"):
            try:
                log_file = log_path
                log_file.write_text("", encoding="utf-8")
                _load_logs()
            except Exception as exc:
                status_var.set(f"清空失败: {exc}")

    clear_btn = tk.Button(top, text="🗑️ 清空", command=_on_clear_log, fg="red")
    clear_btn.pack(side=tk.LEFT)

    # ── 自动轮询刷新 ──
    _last_mtime = 0

    def _poll_log() -> None:
        nonlocal _last_mtime
        if auto_var.get():
            try:
                mtime = log_path.stat().st_mtime
                if mtime > _last_mtime:
                    _last_mtime = mtime
                    _load_logs()
            except OSError:
                pass
        win.after(3000, _poll_log)

    # 初始化加载
    try:
        _last_mtime = log_path.stat().st_mtime
    except OSError:
        pass
    _load_logs()

    # 绑定级别切换自动刷新
    level_combo.bind("<<ComboboxSelected>>", lambda e: _load_logs())

    win.after(3000, _poll_log)
    win.mainloop()


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
            _copy_embedded_config(target_cfg)

        migrate_legacy_data(
            args.room,
            base_dir=BASE_DIR if getattr(sys, "frozen", False) else ".",
        )
        os.chdir(room_dir)
        return str(target_cfg)
    else:
        cfg_path = DATA_DIR / "config.yaml"
        if not cfg_path.exists():
            _copy_embedded_config(cfg_path)

        os.chdir(DATA_DIR)
        return str(cfg_path)


def _copy_embedded_config(dst: Path) -> None:
    """从嵌入式或同级目录复制默认配置到目标路径。"""
    candidates = []
    if getattr(sys, "_MEIPASS", None):
        meipass = Path(sys._MEIPASS)
        candidates.extend([meipass / "config.yaml", meipass / "config.example.yaml"])
    candidates.extend([BASE_DIR / "config.yaml", BASE_DIR / "config.example.yaml"])
    for src in candidates:
        if src.exists():
            import shutil
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            logger.info("copied config from %s -> %s", src, dst)
            return
    logger.warning("no config.yaml found (embedded or at %s), WebUI may not work", BASE_DIR)


# ── 房间状态持久化（重启后自动恢复运行中的房间） ──


_RUNNING_ROOMS_FILE = DATA_DIR / "running_rooms.json"


def _save_running_rooms() -> None:
    """保存当前运行中的房间 ID 列表到文件。"""
    try:
        from app.process_manager import _inproc_bots, _procs
        # 冻结模式用 _inproc_bots，否则用 _procs
        running = _inproc_bots if getattr(sys, "frozen", False) else _procs
        room_ids = list(running.keys())
        _RUNNING_ROOMS_FILE.write_text(json.dumps(room_ids, ensure_ascii=False), encoding="utf-8")
        logger.info("saved %d running rooms: %s", len(room_ids), room_ids)
    except Exception as exc:
        logger.warning("save running rooms failed: %s", exc)


def _load_running_rooms() -> list[str]:
    """读取上次退出时运行中的房间 ID 列表。"""
    try:
        if not _RUNNING_ROOMS_FILE.exists():
            return []
        data = json.loads(_RUNNING_ROOMS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(r) for r in data]
    except Exception as exc:
        logger.warning("load running rooms failed: %s", exc)
    return []


def _restore_running_rooms() -> None:
    """启动上次运行中的房间。"""
    room_ids = _load_running_rooms()
    if not room_ids:
        logger.info("no rooms to restore")
        return

    def _start_rooms() -> None:
        """在新的事件循环中异步启动房间。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            from app.process_manager import start_room_async

            async def _do_restore() -> None:
                for room_id in room_ids:
                    try:
                        logger.info("auto-starting room %s...", room_id)
                        ok = await start_room_async(room_id)
                        logger.info("auto-start room %s: %s", room_id, "OK" if ok else "FAILED")
                    except Exception as exc:
                        logger.warning("auto-start room %s failed: %s", room_id, exc)
                    await asyncio.sleep(0.5)

            loop.run_until_complete(_do_restore())
        finally:
            loop.close()

    threading.Thread(target=_start_rooms, daemon=True, name="restore-rooms").start()


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

    def on_view_logs(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        threading.Thread(target=_view_logs, daemon=True).start()

    def on_exit(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        _save_running_rooms()
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
        pystray.MenuItem("📋 查看日志", on_view_logs),
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

    # 确保 config.yaml 存在（优先嵌入式，降级到 exe 同级）
    cfg_dst = DATA_DIR / "config.yaml"
    if not cfg_dst.exists():
        _copy_embedded_config(cfg_dst)

    # 启动 WebUI — 传入配置路径确保 init_app 能正确解析
    cfg_path = str(DATA_DIR / "config.yaml")
    _start_webui(config_path=cfg_path)

    # 自动恢复之前运行的房间
    _restore_running_rooms()

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
