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
import json
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

# 最近一次子进程退出码（用于向 WebUI 汇报崩溃原因）
_LAST_EXIT_CODE: dict[str, int] = {}


def last_exit_code(room_id: str) -> Optional[int]:
    return _LAST_EXIT_CODE.get(room_id)



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


def _write_bot_error(data_dir: Path, exc: BaseException) -> None:
    """把 Bot 崩溃的「为什么」写进 data_dir/bot_error.json，留给守护进程展示。

    只留异常类型、消息和 traceback 最后三行 —— 完整堆栈在日志里，这里要的是
    用户在界面上能一眼看懂的一句话。
    """
    try:
        import traceback

        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        tail = " / ".join(ln.strip() for ln in tb.strip().splitlines()[-3:])
        detail = f"{type(exc).__name__}: {exc} | {tail}"[:280]
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "bot_error.json").write_text(
            json.dumps({"ts": int(time.time()), "reason": detail}, ensure_ascii=False),
            encoding="utf-8")
    except Exception as exc2:  # noqa: BLE001
        logger.debug("write bot_error failed: %s", exc2)


def _write_bot_error_reason(data_dir: Path, reason: str) -> None:
    """已经有可读原因时直接写，不要再包一层 `RuntimeError: ... | traceback`。

    （`_write_bot_error` 会把异常类型和栈尾拼起来，对「进程秒退」这种
    本身就只有一句话的原因会变成同一个句子重复两遍。）
    """
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "bot_error.json").write_text(
            json.dumps({"ts": int(time.time()), "reason": reason[:400]}, ensure_ascii=False),
            encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.debug("write bot_error reason failed: %s", exc)


async def start_bot_in_process(room_id: str) -> bool:
    """在当前进程内启动 Bot（asyncio Task），返回 True 表示已受理。

    注意：启动不再阻塞等待凭证校验 —— 登录失效时 Bot 会保持运行并写
    auth_status.json，由 WebUI 引导用户重新扫码，登录后自动恢复。
    """
    # 检查是否已在运行
    if _inproc_is_running(room_id):
        logger.info("room %s in-process bot already running", room_id)
        return True

    from app.auth import AuthManager  # noqa: PLC0415
    from app.bot import LiveRobot  # noqa: PLC0415
    from app.config import ConfigError, load_config  # noqa: PLC0415

    root = _get_rooms_dir()
    rooms_dir = root / "rooms" / room_id
    config_path = rooms_dir / "config.yaml"
    if not config_path.exists():
        logger.error("room %s config not found: %s", room_id, config_path)
        return False

    try:
        config = load_config(str(config_path))
    except ConfigError as exc:
        # 配置写错了是**用户能自己修**的问题，不该报一个栈了事。
        # 同时也写进 bot_error.json，让 WebUI 的房间卡片直接把原因显示出来
        # （否则用户只看到「启动失败，请查看该房间的日志」，而 200 行配置里
        # 根本找不到是哪一项）。
        logger.error("room %s 配置有问题：%s", room_id, exc)
        _write_bot_error(rooms_dir / "data", exc)
        return False
    except Exception as exc:
        logger.error("room %s config load failed: %s", room_id, exc, exc_info=True)
        _write_bot_error(rooms_dir / "data", exc)
        return False

    auth = AuthManager(config)
    holder: dict[str, Any] = {"robot": None}

    # ── 房间自己的日志文件 ──
    # 冻结 exe 里 Bot 是同进程 Task，日志全落到托盘那个共享的 ayabot.log 上，
    # `rooms/<id>/bot.log` 从来不存在 —— 于是「日志」标签永远空白，用户报
    # 「机器人莫名其妙关了」时我们手里没有任何现场。子进程模式靠 stdout 重定向
    # 解决这件事，同进程模式必须自己挂 handler。
    from app.logutil import ROOM_CTX, drop_tracked, install_tracked
    room_log_path = rooms_dir / "bot.log"
    try:
        truncate_log_file(room_log_path)   # 和子进程模式一样，先防无限增长
    except Exception as exc:  # noqa: BLE001
        logger.debug("truncate room log failed: %s", exc)
    # 同进程模式以前完全不读 config.runtime.log_level（界面上的「日志级别」
    # 在 exe 里从来没生效过）—— 用户想开 DEBUG 查「统计为什么是 0」时会发现
    # 什么也没多出来。这里按房间生效，并只下调根 logger 的级别。
    _want_level = getattr(logging, str(config.runtime.log_level or "INFO").upper(),
                          logging.INFO)
    install_tracked(room_id, room_log_path, level=_want_level)

    async def _supervised_run() -> None:
        # 上下文变量让同一个 logger 的日志能分清是哪个房间的
        ROOM_CTX.set(room_id)
        try:
            credential = await auth.prepare_credential()
            auth.start_refresh_loop(credential)
            robot = LiveRobot(config=config, credential=credential, auth=auth)
            holder["robot"] = robot
            await robot.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 任何异常都不能让同进程 Bot 静默消失（Windows 冻结模式主要崩溃点）
            logger.error("room %s bot task crashed: %s", room_id, exc, exc_info=True)
            try:
                auth.write_status("error", f"机器人异常退出：{exc}"[:300])
            except Exception:  # noqa: BLE001
                pass
            # 把「为什么退出」留给守护进程展示。守护进程只看到 status!=running，
            # 会写一句「进程已退出」，用户完全不知道是代码 bug、网络问题还是被系统杀了。
            _write_bot_error(Path(config.storage.sqlite_path).parent, exc)
        finally:
            # 任务怎么结束的都要摘：崩溃重启一次就会再挂一组，
            # 不摘的话日志每行写两遍、三遍，用户会以为「机器人一直在崩」。
            drop_tracked(room_id)

    task = asyncio.create_task(_supervised_run(), name=f"bot-{room_id}")
    _inproc_bots[room_id] = (holder, auth, task)
    logger.info("room %s in-process bot started", room_id)
    return True


async def stop_bot_in_process(room_id: str) -> bool:
    """停止当前进程内的 Bot Task。"""
    entry = _inproc_bots.pop(room_id, None)
    if entry is None:
        return True
    holder, auth, task = entry
    try:
        robot = holder.get("robot")
        if robot is not None:
            await robot.shutdown()
        loop_closed = task.get_loop().is_closed()
        task.cancel()
        if not loop_closed:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await auth.stop()
    except Exception as exc:
        logger.warning("room %s in-process stop error: %s", room_id, exc)
    from app.logutil import drop_tracked  # noqa: PLC0415
    drop_tracked(room_id)   # 日志句柄必须关，否则 Windows 上删不掉房间目录
    logger.info("room %s in-process bot stopped", room_id)
    return True


def _inproc_is_running(room_id: str) -> bool:
    """检查同进程 Bot 是否存活（同步安全）。"""
    entry = _inproc_bots.get(room_id)
    if entry is None:
        return False
    _, _, task = entry
    if task.done():
        return False
    # Task 所在的事件循环已关闭 → 它永远不会再执行。典型成因：在别的线程里
    # new_event_loop() 之后立刻 close()（启动期自动恢复房间时的老写法）。
    # 这种「假运行的僵尸 Task」会让上层以为房间在跑，从而跳过真正的启动。
    loop = task.get_loop()
    if loop.is_closed() or not loop.is_running():
        logger.warning(
            "room %s in-process bot 的 Task 所在事件循环已关闭，判定为未运行并清理", room_id
        )
        _inproc_bots.pop(room_id, None)
        return False
    return True


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
    # **父进程必须立刻关掉这个句柄**：它只是给子进程用的 stdout。
    # 不关的话每重启一次就泄漏一个文件句柄，而且 Windows 上
    # 「删除房间」rmtree 会因为 bot.log 还被 WebUI 持有而报 WinError 32。
    try:
        log_fp.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("close log handle failed: %s", exc)
    _pidfile(room_id).write_text(str(proc.pid), encoding="utf-8")
    _procs[room_id] = proc

    # **Popen 成功 ≠ 启动成功**。配置写错、依赖缺失、端口冲突这些都会让子进程在
    # 一两秒内退出，而以前这里立刻 `return True` —— 界面弹「已启动」，
    # 下一秒房间又变回停止，用户看到的是「启动了但是没起来」，而且拿不到原因。
    # （第 24 轮在托盘上修过同一类问题：「启动成功」必须是可验证的状态。）
    # 这里只等到「确认它还活着」就返回，失败则把子进程写下的最后一条错误带回去。
    deadline = time.time() + _START_VERIFY_SECONDS
    while time.time() < deadline:
        if proc.poll() is not None:
            reason = _last_error_from_log(log_path) or f"进程启动后立即退出（exit={proc.returncode}）"
            _procs.pop(room_id, None)
            _pidfile(room_id).unlink(missing_ok=True)
            _write_bot_error_reason(rooms_dir / "data", reason)
            logger.error("room %s 启动后立即退出：%s", room_id, reason)
            return False
        time.sleep(0.1)
    logger.info("room %s started, pid=%d", room_id, proc.pid)
    return True


def _last_error_from_log(path: Path, max_bytes: int = 64 * 1024) -> str:
    """从 bot.log 尾部取出最后一条 ERROR（给「启动后立即退出」用）。

    子进程崩在 `load_config` 里时，bot.log 只有一行可读的 `[ERROR] ...`；
    把那一行返回给用户，比「启动失败，请查看日志」有用得多。
    """
    try:
        if not path.exists():
            return ""
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            text = fh.read().decode("utf-8", errors="replace")
        for line in reversed(text.splitlines()):
            if "[ERROR]" in line or "[CRITICAL]" in line:
                # 去掉时间戳前缀，保留人话部分
                idx = line.find("] ")
                text_part = (line[idx + 2:] if idx > 0 else line).strip()
                # 日志格式是 `时间 [级别] 记录器名: 消息`，把记录器名前缀去掉
                for prefix in ("bili-live-robot: ", "app.process_manager: "):
                    if text_part.startswith(prefix):
                        text_part = text_part[len(prefix):]
                return text_part[:400]
    except Exception:  # noqa: BLE001
        pass
    return ""


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


async def clean_room_async(room_id: str) -> None:
    """彻底清理房间：**先把同进程 Bot 停干净，再交回同步清理**。

    同步版 `clean_room()` 只能把 `_inproc_bots` 里的记账删掉，删不掉那个
    asyncio Task —— 而冻结 exe（绝大多数 Windows 用户）的 Bot 就是这个 Task，
    它开着 `bot.db`。删房间时 rmtree 于是撞上
    `PermissionError: [WinError 32] 另一个程序正在使用此文件` →
    接口 500、目录删一半、界面上房间还在但点进去报错。
    更糟的是 Task 还活着，继续往已删除的目录写统计。

    所以「删房间」这种破坏性动作必须走这个 async 版本：
    先 `await stop_bot_in_process()`（会 robot.shutdown() + cancel Task + auth.stop()），
    再删文件。
    """
    try:
        await stop_bot_in_process(room_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("room %s in-process stop before delete failed: %s", room_id, exc)
    # stop 里已经摘过了，但 Bot 早已不在 _inproc_bots 里（比如崩溃后被守护清账）
    # 时 handler 可能还开着 —— 那会让下面删目录报 WinError 32。再摘一次（幂等）。
    from app.logutil import drop_tracked  # noqa: PLC0415
    drop_tracked(room_id)
    clean_room(room_id)


def clean_room(room_id: str) -> None:
    """彻底清理房间的进程和文件（同步）。

    **只能清理「已经停下」的房间**。房间还在跑时请用 `clean_room_async`：
    这里删不掉同进程 Task，只会让记账和现实脱节（Task 仍在跑、文件仍被占用）。
    """
    entry = _inproc_bots.get(room_id)
    if entry is not None:
        _, _, task = entry
        if not task.done():
            logger.warning(
                "room %s 的同进程 Bot 仍在运行，clean_room() 只移除记账；"
                "调用方应先用 clean_room_async()", room_id)
        _inproc_bots.pop(room_id, None)
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
        _LAST_EXIT_CODE[room_id] = proc.returncode
        logger.warning("room %s process exited (code=%s)", room_id, proc.returncode)

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
#: `start_room()` 启动后等多久确认子进程真的活着。
#: 太短会漏掉慢崩溃，太长会让「启动」按钮卡顿；1.5 秒能覆盖
#: 配置写错 / 依赖缺失 / 端口冲突这类「秒退」情况。
_START_VERIFY_SECONDS = 1.5
#: 单个 bot.log 的字节上限。5000 行正常日志约 1MB；一行的极端情况（整段 traceback）
#: 靠这个上限兜住，否则行数永远不超、文件无限增长。
_MAX_LOG_BYTES = 8 * 1024 * 1024
_MAX_LOG_DAYS = 3
_LOG_CLEANUP_INTERVAL = 3600  # 每小时检查一次


def truncate_log_file(path: Path, max_lines: int = _MAX_LOG_LINES,
                      max_bytes: int = _MAX_LOG_BYTES) -> bool:
    """把日志截断到「最后 max_lines 行」且「不超过 max_bytes」，只读尾部。

    两个上限都必须有（和托盘日志同一个教训）：
    - **只按行数**：一行很长（整段 traceback、单行 JSON）时行数很少却几十 MB，
      文件永远不缩；
    - **读整个文件**：房间每次启动/重启都会调用这里，一个几百 MB 的 bot.log
      每次都被整份读进内存，WebUI 会卡住甚至 OOM。

    所以大文件按块从尾部读，凑够 max_lines 行或 max_bytes 字节就停。
    """
    if not path.exists():
        return True
    try:
        size = path.stat().st_size
        if size == 0:
            return True
        if size <= max_bytes:
            # 小文件：行数超了才动
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) <= max_lines:
                return True
            lines = lines[-max_lines:]
        else:
            # 大文件：只从尾部读，最多读到 max_bytes 的一倍多（留余量给不完整的首行）
            budget = min(size, max(int(max_bytes * 1.2), 64 * 1024))
            with path.open("rb") as fh:
                fh.seek(size - budget)
                raw = fh.read(budget)
            text = raw.decode("utf-8", errors="replace")
            if size > budget:
                # 只有换行后面**还有内容**时才丢首行。整块就是某一行超长尾巴时
                # （换行在末尾），丢首行会把内容全丢掉 —— 别再犯这个错。
                nl = text.find("\n")
                if nl >= 0 and nl + 1 < len(text):
                    text = text[nl + 1:]
            lines = text.splitlines()
            if len(lines) > max_lines:
                lines = lines[-max_lines:]
            # 行数没超但字节还超：继续从尾部裁
            kept: list[str] = []
            used = 0
            for ln in reversed(lines):
                cost = len(ln.encode("utf-8")) + 1
                if used + cost > max_bytes:
                    break
                kept.append(ln)
                used += cost
            lines = list(reversed(kept))
            if not lines:
                # 单行本身就超过上限：保留这一行的**尾部**（最后一行通常是
                # `RuntimeError: xxx`，比整段开头更有用），而不是丢掉全部内容
                last = (text.splitlines() or [""])[-1]
                # 留余量给行尾换行：Windows 上 write_text 会把 LF 写成 CRLF，
                # 按「字节数恰好等于上限」切会在落盘后超 1 字节。
                last_b = last.encode("utf-8")[-(max_bytes - 4):]
                lines = [last_b.decode("utf-8", errors="replace")]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("truncated %s: %d B -> %d B (%d lines)",
                    path.name, size, path.stat().st_size, len(lines))
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


#: 除了清日志，还要定期做的事（图片缓存清理等）。由 server 注册。
_PERIODIC_TASKS: list[tuple[object, str]] = []


def register_periodic_task(fn, name: str = "") -> None:
    """注册「每小时跑一次」的维护任务。

    为什么需要这个入口：图片代理缓存原来只在 `init_app` 里清一次。
    exe 用户的正常形态是开几周不重启，于是缓存目录只涨不清 ——
    一场热门直播能缓存上千张头像，几个月下来是几个 GB。
    """
    _PERIODIC_TASKS.append((fn, name or getattr(fn, "__name__", "task")))


def start_periodic_log_cleanup() -> None:
    """启动后台线程，定期清理过期/过大的日志文件与缓存。"""
    thread = threading.Thread(target=_log_cleanup_loop, daemon=True, name="log-cleanup")
    thread.start()
    logger.info("periodic log cleanup started (interval=%ds, max_days=%d)",
                _LOG_CLEANUP_INTERVAL, _MAX_LOG_DAYS)


def _log_cleanup_loop() -> None:
    """定时清理循环（日志 + 所有注册进来的维护任务）。"""
    while True:
        try:
            count = cleanup_old_logs()
            if count:
                logger.info("log cleanup: removed %d old log files", count)
        except Exception as exc:
            logger.warning("log cleanup error: %s", exc)
        # 注册进来的任务各自 try：一个坏了不能把日志清理也带下去
        for fn, name in list(_PERIODIC_TASKS):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                logger.warning("periodic task %s failed: %s", name, exc)
        time.sleep(_LOG_CLEANUP_INTERVAL)
