"""
Ayabot 跨平台进程管理器
────────────────────
统一管理 Bot 子进程的启动/停止/状态查询。

平台兼容：
  - Linux / macOS：Popen + signal
  - Windows：Popen + terminate()
  - Docker / Podman：容器外只需跟踪 PID
  - 无 root 依赖，零权限要求

冻结模式（PyInstaller .exe）：
  改用 in-process asyncio Task 运行 Bot，因为 .exe 无法作为 Python 解释器
  启动子进程。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("bili-live-robot.process_manager")

# ── 全局运行时状态 ──────────────────────────────────────────
# 进程 PID 持久化到 rooms/<id>/bot.pid，进程对象在 WebUI 生命周期内保持
_procs: dict[str, subprocess.Popen] = {}

# 房间根目录（可由 set_rooms_base_dir 覆盖，兼容 PyInstaller 打包）
_rooms_base_dir: Path | None = None

# 同进程 Bot 实例（PyInstaller 冻结模式）
# room_id -> (robot, auth_manager, asyncio.Task)
_inproc_bots: dict[str, tuple[Any, Any, asyncio.Task]] = {}


def set_rooms_base_dir(dir_path: str | Path) -> None:
    """设置 rooms/ 的基础目录路径（覆盖默认的自动检测）。"""
    global _rooms_base_dir
    _rooms_base_dir = Path(dir_path).resolve()


def _get_rooms_dir() -> Path:
    """返回 rooms/ 上级目录（项目根或自定义数据目录）。"""
    if _rooms_base_dir is not None:
        return _rooms_base_dir
    return Path(__file__).resolve().parent.parent


def _pidfile(room_id: str) -> Path:
    return _get_rooms_dir() / "rooms" / room_id / "bot.pid"


def _lockfile(room_id: str) -> Path:
    return _get_rooms_dir() / "rooms" / room_id / "bot.lock"


def _rooms_dir_path() -> Path:
    """返回 rooms/ 目录的绝对路径。"""
    return _get_rooms_dir() / "rooms"


def cleanup_all_stale_pidfiles() -> int:
    """扫描 rooms/ 目录，清理所有残留的 bot.pid 和 bot.lock 文件。
    
    在容器/服务重启时调用，确保旧会话的 PID 文件不会导致状态误判。
    返回清理的文件总数。
    """
    rooms_dir = _rooms_dir_path()
    if not rooms_dir.exists():
        return 0
    cleaned = 0
    for d in rooms_dir.iterdir():
        if not d.is_dir():
            continue
        pidf = d / "bot.pid"
        if pidf.exists():
            try:
                pidf.unlink()
                cleaned += 1
            except OSError as exc:
                logger.warning("cleanup pidfile %s failed: %s", pidf, exc)
        lockf = d / "bot.lock"
        if lockf.exists():
            try:
                lockf.unlink()
                cleaned += 1
            except OSError as exc:
                logger.warning("cleanup lockfile %s failed: %s", lockf, exc)
    if cleaned:
        logger.info("cleaned %d stale pid/lock files from %s", cleaned, rooms_dir)
    return cleaned


# ── 自动检测是否使用同进程模式 ──


def _use_inprocess() -> bool:
    """冻结模式（PyInstaller .exe）下使用同进程运行 Bot。"""
    return getattr(sys, "frozen", False)


# ── 同进程 Bot 运行（冻结模式） ──────────────────────────────


async def start_bot_in_process(room_id: str) -> bool:
    """在当前进程内启动 Bot（asyncio Task），返回 True 表示成功。"""
    # 检查是否已在运行
    if _inproc_is_running(room_id):
        logger.info("room %s in-process bot already running", room_id)
        return True

    from app.auth import AuthManager  # noqa: PLC0415
    from app.bot import LiveRobot  # noqa: PLC0415
    from app.config import load_config  # noqa: PLC0415

    root = _get_rooms_dir()
    rooms_dir = root / "rooms" / room_id
    config_path = rooms_dir / "config.yaml"
    if not config_path.exists():
        logger.error("room %s config not found: %s", room_id, config_path)
        return False

    try:
        config = load_config(str(config_path))
        auth = AuthManager(config)
        credential = await auth.prepare_credential()
        auth.start_refresh_loop(credential)

        robot = LiveRobot(config=config, credential=credential)
        task = asyncio.create_task(robot.run())
        _inproc_bots[room_id] = (robot, auth, task)
        logger.info("room %s in-process bot started", room_id)
        return True
    except Exception as exc:
        logger.error("room %s in-process start failed: %s", room_id, exc, exc_info=True)
        return False


async def stop_bot_in_process(room_id: str) -> bool:
    """停止当前进程内的 Bot Task。"""
    entry = _inproc_bots.pop(room_id, None)
    if entry is None:
        return True
    robot, auth, task = entry
    try:
        await robot.shutdown()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await auth.stop()
    except Exception as exc:
        logger.warning("room %s in-process stop error: %s", room_id, exc)
    logger.info("room %s in-process bot stopped", room_id)
    return True


def _inproc_is_running(room_id: str) -> bool:
    """检查同进程 Bot 是否存活（同步安全）。"""
    entry = _inproc_bots.get(room_id)
    if entry is None:
        return False
    _, _, task = entry
    return not task.done()


# ── 子进程模式（常规 Python 运行） ──────────────────────────


def start_room(room_id: str) -> bool:
    """启动房间 Bot 子进程。返回 True 表示成功启动。"""
    # 检查是否已在运行
    if _is_running(room_id):
        logger.info("room %s already running, skip start", room_id)
        return True

    root = _get_rooms_dir()
    rooms_dir = root / "rooms" / room_id
    config_path = rooms_dir / "config.yaml"
    if not config_path.exists():
        logger.error("room %s config not found: %s", room_id, config_path)
        return False

    log_path = rooms_dir / "bot.log"
    # 追加前先截断，防止无限增长
    truncate_log_file(log_path)
    try:
        log_fp = log_path.open("a", encoding="utf-8")
    except OSError as exc:
        logger.error("room %s cannot open log file: %s", room_id, exc)
        return False

    # 用 subprocess.Popen 启动子进程
    # 设置 PYTHONIOENCODING=utf-8 防止 Windows 下中文编码为 cp936 导致日志乱码
    env = os.environ.copy()
    if "PYTHONIOENCODING" not in env:
        env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.Popen(
            [
                sys_executable(),
                "-m",
                "app.main",
                "--room",
                room_id,
            ],
            cwd=str(root),
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            # 跨平台：创建新进程组便于信号管理
            creationflags=_creation_flags(),
            start_new_session=True,
        )
    except Exception as exc:
        logger.error("room %s start failed: %s", room_id, exc)
        log_fp.close()
        return False

    # 记录 PID 到文件
    _pidfile(room_id).write_text(str(proc.pid), encoding="utf-8")
    _procs[room_id] = proc
    logger.info("room %s started, pid=%d", room_id, proc.pid)
    return True


def stop_room(room_id: str) -> bool:
    """停止房间 Bot 子进程。返回 True 表示已停止。"""
    proc = _procs.pop(room_id, None)
    pid = _read_pid(room_id)

    # 尝试从进程对象终止
    if proc is not None and proc.poll() is None:
        _terminate_proc(proc, room_id, pid or proc.pid)

    # 如果进程对象不存在但 PID 文件还在，尝试查杀
    if pid:
        _kill_by_pid(pid, room_id)

    # 清理残留文件
    _pidfile(room_id).unlink(missing_ok=True)
    _lockfile(room_id).unlink(missing_ok=True)
    logger.info("room %s stopped", room_id)
    return True


def restart_room(room_id: str) -> bool:
    """重启房间 Bot。"""
    stop_room(room_id)
    # 等待进程完全退出
    for _ in range(10):
        if not _is_running(room_id):
            break
        time.sleep(0.3)
    return start_room(room_id)


def room_status(room_id: str) -> str:
    """查询房间 Bot 状态: 'running' 或 'stopped'."""
    return "running" if _is_running(room_id) else "stopped"


def clean_room(room_id: str) -> None:
    """彻底清理房间的进程和文件。"""
    # 先清理同进程 Bot
    if _inproc_bots.pop(room_id, None) is not None:
        logger.info("room %s in-process bot entry cleaned", room_id)
    stop_room(room_id)
    pidf = _pidfile(room_id)
    if pidf.exists():
        pidf.unlink()
    lockf = _lockfile(room_id)
    if lockf.exists():
        lockf.unlink()
    _procs.pop(room_id, None)


# ── Async API（自动选择子进程/同进程） ──────────────────────


async def start_room_async(room_id: str) -> bool:
    """启动房间 Bot：冻结模式用同进程 Task，否则用子进程。"""
    if _use_inprocess():
        return await start_bot_in_process(room_id)
    return start_room(room_id)


async def stop_room_async(room_id: str) -> bool:
    """停止房间 Bot：同时尝试同进程和子进程两种方式。"""
    await stop_bot_in_process(room_id)
    stop_room(room_id)
    return True


async def restart_room_async(room_id: str) -> bool:
    """重启房间 Bot（async 版本）。"""
    await stop_room_async(room_id)
    for _ in range(10):
        if not _is_running(room_id):
            break
        await asyncio.sleep(0.3)
    return await start_room_async(room_id)


# ── 内部实现 ────────────────────────────────────────────────


def _is_running(room_id: str) -> bool:
    """判断进程是否存活（检查同进程 Bot + 子进程 + PID 文件）。"""
    # 0) 检查同进程 Bot
    if _inproc_is_running(room_id):
        return True

    # 1) 检查内存中的进程对象
    proc = _procs.get(room_id)
    if proc is not None:
        alive = proc.poll() is None
        if alive:
            return True
        # 进程已退出，清理
        _procs.pop(room_id, None)

    # 2) 检查 PID 文件
    pid = _read_pid(room_id)
    if pid is None:
        return False

    if _pid_alive(pid):
        return True

    # PID 文件残留，清理
    _pidfile(room_id).unlink(missing_ok=True)
    return False


def _read_pid(room_id: str) -> Optional[int]:
    pidf = _pidfile(room_id)
    if not pidf.exists():
        return None
    try:
        pid = int(pidf.read_text(encoding="utf-8").strip())
        return pid if pid > 0 else None
    except (ValueError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    """跨平台检查 PID 是否存活。"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_proc(proc: subprocess.Popen, room_id: str, pid: int) -> None:
    """先 SIGTERM，等不到就 SIGKILL。"""
    try:
        if os.name == "nt":
            proc.terminate()  # Windows: TerminateProcess
        else:
            os.kill(pid, signal.SIGTERM)
        # 等待最多 5 秒
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("room %s pid=%d force kill", room_id, pid)
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.kill(pid, signal.SIGKILL)
            proc.wait(timeout=3)
        except Exception:
            pass
    except Exception:
        pass


def _kill_by_pid(pid: int, room_id: str) -> None:
    """仅通过 PID 号杀进程（进程对象不可用时）。"""
    if not _pid_alive(pid):
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
        else:
            os.kill(pid, signal.SIGTERM)
            # 等 3 秒，不行就 SIGKILL
            for _ in range(6):
                time.sleep(0.5)
                if not _pid_alive(pid):
                    return
            os.kill(pid, signal.SIGKILL)
    except Exception as exc:
        logger.warning("room %s kill pid=%d failed: %s", room_id, pid, exc)


def _creation_flags() -> int:
    """Windows 下创建新进程组，避免 Ctrl+C 传到子进程。"""
    if os.name == "nt":
        import subprocess as _sp
        return getattr(_sp, "CREATE_NEW_PROCESS_GROUP", 0)
    return 0


def sys_executable() -> str:
    """获取当前 Python 解释器路径（兼容虚拟环境）。"""
    import sys
    return sys.executable


# ── 日志清理（行数轮转 + 时间过期删除） ────────────────────

_MAX_LOG_LINES = 5000
_MAX_LOG_DAYS = 3
_LOG_CLEANUP_INTERVAL = 3600  # 每小时检查一次


def truncate_log_file(path: Path, max_lines: int = _MAX_LOG_LINES) -> bool:
    """将日志文件截断为最后 max_lines 行。"""
    if not path.exists() or path.stat().st_size == 0:
        return True
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            tail = lines[-max_lines:]
            path.write_text("\n".join(tail) + "\n", encoding="utf-8")
            logger.info("truncated %s: %d -> %d lines", path.name, len(lines), max_lines)
        return True
    except Exception as exc:
        logger.warning("truncate log failed %s: %s", path, exc)
        return False


def cleanup_old_logs(max_days: int = _MAX_LOG_DAYS) -> int:
    """删除超过 max_days 的 bot.log，截断过大的 bot.log。返回清理的文件数。"""
    cutoff = time.time() - max_days * 86400
    cleaned = 0

    # 扫描 rooms/*/bot.log
    rooms_dir = _get_rooms_dir() / "rooms"
    if not rooms_dir.exists():
        return 0

    for d in rooms_dir.iterdir():
        if not d.is_dir():
            continue
        log_file = d / "bot.log"
        if not log_file.exists():
            continue
        # 按时间清理
        try:
            mtime = log_file.stat().st_mtime
            if mtime < cutoff:
                log_file.unlink(missing_ok=True)
                logger.info("deleted old log: %s (mtime=%s)", log_file,
                            datetime.fromtimestamp(mtime).isoformat())
                cleaned += 1
                continue
        except OSError:
            pass
        # 按行数截断
        truncate_log_file(log_file)

    return cleaned


def start_periodic_log_cleanup() -> None:
    """启动后台线程，定期清理过期/过大的日志文件。"""
    thread = threading.Thread(target=_log_cleanup_loop, daemon=True, name="log-cleanup")
    thread.start()
    logger.info("periodic log cleanup started (interval=%ds, max_days=%d)",
                _LOG_CLEANUP_INTERVAL, _MAX_LOG_DAYS)


def _log_cleanup_loop() -> None:
    """定时清理循环。"""
    while True:
        try:
            count = cleanup_old_logs()
            if count:
                logger.info("log cleanup: removed %d old log files", count)
        except Exception as exc:
            logger.warning("log cleanup error: %s", exc)
        time.sleep(_LOG_CLEANUP_INTERVAL)
