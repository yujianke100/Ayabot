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
    """原子写。截断的 port.txt 会让下次启动退回默认端口，
    用户收藏的地址（以及所有写着那个端口的文档）就都失效了。"""
    from app.config import atomic_write_text
    atomic_write_text(_PORT_FILE, str(port))


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
# 命令行 --port 必须生效（以前 args.port 解析出来就没人用，写的是「设置端口」菜单那条路径）
if getattr(args, "port", None):
    if 1024 <= int(args.port) <= 65535:
        _port = int(args.port)
    else:
        print(f"--port {args.port} 不合法（需 1024-65535），忽略")
_stop_event = threading.Event()

# ── In-process uvicorn ──


#: 配置文件的**绝对**路径。托盘自己的 `_restart_webui()` 以前调
#: `_start_webui()` 不传参数，于是它去加载相对路径 "config.yaml" —— 冻结模式下
#: 当前目录是「用户双击 exe 时所在的目录」（下载目录、桌面、System32…），
#: 那里通常没有 config.yaml，`load_config` 直接抛异常、`init_app` 失败、
#: WebUI 静默消失，而「设置端口」的弹窗还在说「已切换成功」。
#: 所有启动路径都必须走这个常量。
_RESOLVED_CONFIG_PATH: Path = DATA_DIR / "config.yaml"


def _port_serving(port: int, timeout: float = 1.0) -> bool:
    """端口上真的有人在监听吗。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _start_webui(config_path: str | None = None) -> bool:
    """在后台线程启动 WebUI（in-process，兼容 PyInstaller），返回是否真的起来了。

    **必须返回 bool**：以前它在 `server.run()` 之前就 `q.put("ready")`，
    于是「端口被占用 / 绑定失败 / 线程立刻死掉」这些情况一律被当成启动成功：
    托盘图标正常出现、浏览器打开是空白页、日志里只有一行 uvicorn 的 traceback，
    用户看到的就是「双击了没反应 / 后台打不开」。
    """
    import uvicorn
    from app.web.server import app, init_app

    # PyInstaller windowed 模式下 sys.stderr/stdout 为 None，uvicorn logging 会崩溃
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")

    cfg_file = str(config_path or _RESOLVED_CONFIG_PATH)

    # 关键: 初始化配置 (DB路径、认证信息、LLM配置等)
    try:
        from app.config import load_config
        cfg = load_config(cfg_file)
        init_app(config=cfg, config_path=cfg_file)
        logger.info("init_app done, config_path=%s", cfg_file)
    except Exception as exc:
        logger.error("init_app failed: %s", exc, exc_info=True)
        _start_webui._last_error = f"读取配置失败：{exc}"
        return False

    _start_webui._intentional_stop = False
    _start_webui._last_error = ""
    logger.info("starting WebUI in-process: port=%s", _port)

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
            server.run()
        except SystemExit as exc:
            # uvicorn 在端口绑定失败时走 sys.exit(1)
            _start_webui._last_error = f"端口 {_port} 绑定失败（可能被其他程序占用）"
            logger.error("uvicorn exited: %s (port=%s)", exc, _port)
        except Exception as exc:  # noqa: BLE001
            _start_webui._last_error = f"WebUI 线程异常：{exc}"
            logger.error("uvicorn thread crashed", exc_info=True)

    t = threading.Thread(target=_run, daemon=True, name="uvicorn")
    _start_webui._thread = t
    t.start()

    # 只有 uvicorn 真的完成 startup（server.started=True）才算成功
    deadline = time.time() + 15
    while time.time() < deadline:
        server = getattr(_start_webui, "_server", None)
        if getattr(server, "started", False):
            logger.info("WebUI started (in-process) on port %s", _port)
            return True
        if not t.is_alive():
            break
        time.sleep(0.05)

    if not _start_webui._last_error:
        _start_webui._last_error = f"WebUI 启动超时（15s，端口 {_port}）"
    logger.error("WebUI failed to start: %s", _start_webui._last_error)
    return False


def _webui_failure_hint(err: str) -> str:
    """把启动失败翻译成用户能照做的一句话。"""
    if "绑定失败" in err or "占用" in err or "10048" in err:
        return (
            f"WebUI 端口 {_port} 被其他程序占用了，后台打不开。\n\n"
            f"处理办法：右键托盘图标 → 设置端口，换一个别的端口（比如 {_port + 1}）。"
        )
    if "读取配置失败" in err:
        return (
            f"读取配置文件失败：\n{DATA_DIR / 'config.yaml'}\n\n"
            "处理办法：确认该文件存在且格式正确（可以对比 config.example.yaml）。"
        )
    return f"WebUI 启动失败：{err}"


def _stop_webui() -> None:
    _start_webui._intentional_stop = True
    server = getattr(_start_webui, "_server", None)
    thread = getattr(_start_webui, "_thread", None)
    if server:
        logger.info("stopping WebUI...")
        server.should_exit = True
        # 不能只设置标志就返回：改端口时紧接着要在**同一个端口**上重新监听，
        # 旧 server 还没释放就会绑定失败。等它真的停（最多 8 秒）。
        for _ in range(80):
            if thread is None or not thread.is_alive():
                break
            time.sleep(0.1)
        logger.info(
            "WebUI stop signaled (thread alive=%s)",
            bool(thread is not None and thread.is_alive()),
        )


def _restart_webui(config_path: str | None = None) -> bool:
    """停掉再起，返回新实例是否真的起来了。"""
    _stop_webui()
    return _start_webui(config_path or str(_RESOLVED_CONFIG_PATH))


def _webui_watchdog() -> None:
    """WebUI 线程意外死亡时自动重启（托盘还活着、后台已经没了的兜底）。

    和第 14 轮修「后台任务静默死亡」是同一类问题，只是这一层发生在托盘进程里：
    uvicorn 线程一旦退出，托盘图标还在、菜单还能点，但所有房间机器人都没了，
    用户看到的正是「机器人莫名其妙关了」。
    """
    restarts: list[float] = []
    gave_up = False
    while not _stop_event.is_set():
        time.sleep(20)
        try:
            if getattr(_start_webui, "_intentional_stop", False):
                continue
            server = getattr(_start_webui, "_server", None)
            if server is None:
                continue
            if getattr(server, "started", False) and not server.should_exit:
                continue
            now = time.time()
            restarts = [x for x in restarts if now - x < 600]
            if len(restarts) >= 3:
                if not gave_up:
                    gave_up = True
                    logger.error("WebUI 连续 %d 次意外停止，已放弃自动重启", len(restarts))
                    threading.Thread(
                        target=_show_message,
                        args=(
                            "Ayabot 后台已停止",
                            "WebUI 反复异常停止，已停止自动重启。\n\n"
                            f"请查看日志：{log_path}\n"
                            "如果反复出现，退出软件重新打开一次；仍不行请把日志发给作者。",
                        ),
                        daemon=True,
                    ).start()
                continue
            restarts.append(now)
            logger.warning(
                "检测到 WebUI 已停止（started=%s），尝试第 %d 次自动重启",
                getattr(server, "started", False), len(restarts),
            )
            if not _restart_webui():
                logger.error("自动重启失败：%s", getattr(_start_webui, "_last_error", ""))
        except Exception as exc:  # noqa: BLE001
            logger.debug("webui watchdog error: %s", exc)


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
        old_port = _port
        _port = new_port
        if _restart_webui():
            _save_port(_port)
            _show_message("端口已更改", f"WebUI 已切换到端口 {_port}")
        else:
            # 起不来就把端口退回原值：否则下次启动也从坏端口起，
            # 用户会连托盘菜单都以为「已经改好了」。
            _port = old_port
            if _port_serving(old_port):
                _save_port(old_port)
            err = getattr(_start_webui, "_last_error", "")
            _show_message(
                "端口未生效",
                f"切到端口 {new_port} 失败：{err or '未知原因'}\n\n"
                f"已退回原来的端口 {old_port}。换个端口再试（建议 20000 以上）。",
            )
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
        ico = Path(sys._MEIPASS) / "figs" / "icon.png" if getattr(sys, "_MEIPASS", None) else BASE_DIR / "figs" / "icon.png"
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
            try:
                from app.config import atomic_write_text
                # 原子写：半截 config.yaml 会让首次启动直接失败（而且要人工修）
                atomic_write_text(dst, src.read_text(encoding="utf-8"))
                logger.info("copied config from %s -> %s", src, dst)
            except Exception as exc:  # noqa: BLE001
                logger.error("copy config failed: %s", exc)
            return
    logger.warning("no config.yaml found (embedded or at %s), WebUI may not work", BASE_DIR)


# ── 房间状态持久化（重启后自动恢复运行中的房间） ──


_RUNNING_ROOMS_FILE = DATA_DIR / "running_rooms.json"


def _save_running_rooms() -> None:
    """把当前正在运行的房间 ID 记到 running_rooms.json（仅作历史备份/迁移线索）。

    注意：**启动时不再由托盘自己恢复房间**。恢复统一交给 WebUI 进程里的
    desired_states.json + supervisor（见 app/web/server.py 的 _auto_start_rooms），
    因为 Bot 的 asyncio Task 必须建在 uvicorn 的事件循环上。旧实现在托盘线程里
    新建事件循环再 close()，会留下「Task 已死但状态是 running」的僵尸，导致
    打开软件后房间根本不启动。
    """
    try:
        from app.process_manager import _inproc_bots, _procs
        # 冻结模式用 _inproc_bots，否则用 _procs
        running = _inproc_bots if getattr(sys, "frozen", False) else _procs
        room_ids = sorted(running.keys())
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
    """已废弃：不再由托盘自己启动房间。

    恢复统一交给 WebUI 侧的 `_auto_start_rooms()`（读 data/desired_states.json，
    在 uvicorn 的事件循环里启动）。这里保留空实现，避免旧调用点报错。
    """
    room_ids = _load_running_rooms()
    if room_ids:
        logger.info(
            "running_rooms.json 里有 %d 个房间，交由 WebUI 侧的期望状态自动启动: %s",
            len(room_ids), room_ids,
        )


def _create_tray_icon() -> bool:
    """创建系统托盘图标。返回 False 表示托盘没能起来（调用方必须继续提供服务）。"""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        logger.error("pystray or Pillow not installed. Run: pip install pystray pillow")
        return False

    # 生成图标 — 按优先级搜索多个位置和文件名
    icon_size = 64
    icon_img = Image.new("RGBA", (icon_size, icon_size), (0, 0, 0, 0))

    icon_candidates = []
    # 1) PyInstaller 打包目录 (_MEIPASS)
    if getattr(sys, "_MEIPASS", None):
        meipass = Path(sys._MEIPASS)
        icon_candidates.extend([
            meipass / "figs" / "icon.png",
            meipass / "figs" / "logo.png",
        ])
    # 2) .exe 同级目录
    icon_candidates += [
        BASE_DIR / "figs" / "icon.png",
        BASE_DIR / "figs" / "logo.png",
        BASE_DIR / "assets" / "figs" / "icon.png",
        BASE_DIR / "assets" / "figs" / "logo.png",
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
    return True


def _unblock_self() -> None:
    """去掉 exe 上的 Zone.Identifier（「下载自 Internet」标记）。

    从浏览器下载的 exe 会被打上这个备用数据流，于是每次双击都弹
    「无法验证发布者。你确定要运行此软件吗？」。用户点过第一次「运行」之后
    我们主动清掉标记，后续启动就不再弹（和手工「右键→属性→解除锁定」等价）。
    这只是绕开提示，不等于代码签名 —— 要彻底消除需要 Authenticode 证书。
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        exe = str(Path(sys.executable))
        stream = exe + ":Zone.Identifier"
        if not os.path.exists(stream):
            return
        ok = ctypes.windll.kernel32.DeleteFileW(ctypes.c_wchar_p(stream))
        if ok:
            logger.info("已解除 exe 的下载标记（下次启动不再弹发布者警告）")
        else:
            logger.info("解除下载标记失败（err=%s），下次启动仍会提示", ctypes.get_last_error())
    except Exception as exc:  # noqa: BLE001
        logger.debug("unblock self failed: %s", exc)


def main() -> None:
    # 单实例检查
    if not _acquire_lock():
        # 以前是直接 return：用户双击第二次，屏幕上一**点反应都没有**，
        # 很容易被当成「软件坏了/自己关掉了」，然后去任务管理器强杀。
        # 已经有实例在跑时，正确反应是把他的浏览器指过去。
        logger.warning("another instance already running, opening existing WebUI")
        port = _read_port()
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(
            target=_show_message,
            args=(
                "Ayabot 已经在运行",
                f"已经有一个 Ayabot 在后台运行（端口 {port}）。\n\n"
                f"已经帮你打开了管理后台：http://127.0.0.1:{port}/\n\n"
                "如果浏览器没打开，请手动访问上面的地址。\n"
                "要重启软件：先右键托盘图标 → 退出，再重新打开。",
            ),
            daemon=True,
        ).start()
        # 留一点时间让弹窗线程起来
        time.sleep(1.5)
        return

    _unblock_self()

    logger.info("Ayabot starting… data dir: %s", DATA_DIR)
    # 命令行给了端口就记住它（否则用户以为「设好了」，下次启动又跑回旧端口）
    if getattr(args, "port", None):
        try:
            _save_port(_port)
            logger.info("port from CLI persisted: %s", _port)
        except Exception as exc:  # noqa: BLE001
            logger.warning("persist CLI port failed: %s", exc)

    # 确保 config.yaml 存在（优先嵌入式，降级到 exe 同级）
    cfg_dst = DATA_DIR / "config.yaml"
    if not cfg_dst.exists():
        _copy_embedded_config(cfg_dst)

    # 启动 WebUI — 传入配置路径确保 init_app 能正确解析
    cfg_path = str(DATA_DIR / "config.yaml")
    webui_ok = _start_webui(config_path=cfg_path)
    if not webui_ok:
        # 不能只写日志：windowed 模式没有控制台，用户只会看到「托盘图标在、
        # 点开后台是空白」。必须弹窗告诉他怎么办。
        err = getattr(_start_webui, "_last_error", "")
        logger.error("首次启动 WebUI 失败：%s", err)
        threading.Thread(
            target=_show_message,
            args=("Ayabot 后台启动失败", _webui_failure_hint(err)),
            daemon=True,
        ).start()
    # WebUI 线程意外死亡的自愈（托盘还活着但后台没了）
    threading.Thread(target=_webui_watchdog, daemon=True, name="webui-watchdog").start()

    # 自动恢复之前运行的房间
    _restore_running_rooms()

    # 延迟打开浏览器
    threading.Thread(target=_delayed_open_browser, daemon=True).start()

    # 主线程：显示托盘图标
    logger.info("tray icon starting…")
    try:
        tray_ok = _create_tray_icon()
    except Exception as exc:  # noqa: BLE001
        # pystray 在无桌面会话（远程/服务方式启动）、图标被占用、
        # 或者 pillow 版本不兼容时会直接抛。以前异常冒出去 → main() 结束 →
        # 进程退出 → 后台 WebUI 和所有房间机器人一起消失，
        # 而且 --windowed 模式下连一行提示都没有，用户看到的就是
        # 「软件自己关了」。托盘只是外壳，它挂了不该带走后台。
        logger.error("托盘图标创建失败: %s", exc, exc_info=True)
        tray_ok = False
    if not tray_ok:
        logger.error("托盘不可用，改为纯后台运行（WebUI 仍在 http://127.0.0.1:%s/）", _port)
        threading.Thread(
            target=_show_message,
            args=(
                "Ayabot 已在后台运行（托盘图标不可用）",
                f"系统托盘图标没能显示，但后台服务正常运行。\n\n"
                f"请在浏览器打开：http://127.0.0.1:{_port}/\n\n"
                "要退出软件，请在任务管理器结束 Ayabot 进程，或重启电脑。",
            ),
            daemon=True,
        ).start()
        # 主线程停在这里，让 uvicorn 线程和房间机器人继续跑
        _stop_event.wait()


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
