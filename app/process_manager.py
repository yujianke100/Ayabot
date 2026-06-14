"""
Ayabot 跨平台进程管理器
────────────────────
统一管理 Bot 子进程的启动/停止/状态查询。

平台兼容：
  - Linux / macOS：Popen + signal
  - Windows：Popen + terminate()
  - Docker / Podman：容器外只需跟踪 PID
  - 无 root 依赖，零权限要求
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bili-live-robot.process_manager")

# ── 全局运行时状态 ──────────────────────────────────────────
# 进程 PID 持久化到 rooms/<id>/bot.pid，进程对象在 WebUI 生命周期内保持
_procs: dict[str, subprocess.Popen] = {}


def _get_project_root() -> Path:
    """返回项目根目录（包含 app/ 和 rooms/ 的目录）."""
    return Path(__file__).resolve().parent.parent


def _pidfile(room_id: str) -> Path:
    return _get_project_root() / "rooms" / room_id / "bot.pid"


def _lockfile(room_id: str) -> Path:
    return _get_project_root() / "rooms" / room_id / "bot.lock"


# ── 核心 API ────────────────────────────────────────────────


def start_room(room_id: str) -> bool:
    """启动房间 Bot 子进程。返回 True 表示成功启动。"""
    # 检查是否已在运行
    if _is_running(room_id):
        logger.info("room %s already running, skip start", room_id)
        return True

    root = _get_project_root()
    rooms_dir = root / "rooms" / room_id
    config_path = rooms_dir / "config.yaml"
    if not config_path.exists():
        logger.error("room %s config not found: %s", room_id, config_path)
        return False

    log_path = rooms_dir / "bot.log"
    try:
        log_fp = log_path.open("a", encoding="utf-8")
    except OSError as exc:
        logger.error("room %s cannot open log file: %s", room_id, exc)
        return False

    # 用 subprocess.Popen 启动子进程
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
    stop_room(room_id)
    pidf = _pidfile(room_id)
    if pidf.exists():
        pidf.unlink()
    lockf = _lockfile(room_id)
    if lockf.exists():
        lockf.unlink()
    _procs.pop(room_id, None)


# ── 内部实现 ────────────────────────────────────────────────


def _is_running(room_id: str) -> bool:
    """判断进程是否存活（优先用进程对象，降级到 PID 文件）。"""
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
