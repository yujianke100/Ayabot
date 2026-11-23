"""第二十四轮自检：托盘程序的 WebUI 启停链路（Windows 启动路径）。

本轮背景（对应「机器人莫名其妙关闭 / 双击没反应」剩下的那条路径）：

1. `_start_webui()` 在 `server.run()` **之前**就 `q.put("ready")` 并返回。
   端口被占用、绑定失败、uvicorn 线程立刻死掉，全都算「启动成功」：
   托盘图标正常出现、浏览器打开是空白页、日志里只有一行 uvicorn 的 traceback。
   实测（端口被占用）：`_start_webui` 正常返回，`server.started=False`，
   uvicorn 打了 `[Errno 10048]` 然后自己退出。

2. `_restart_webui()` 调 `_start_webui()` **不带参数** → 兜底成相对路径
   `"config.yaml"`。冻结模式下当前目录是「双击 exe 时所在的目录」，
   那里通常没有 config.yaml → `load_config` 抛 FileNotFoundError →
   `init_app` 失败 → WebUI 静默消失，而「设置端口」的弹窗还在说「已切换成功」。

3. `_stop_webui()` 只设置 `should_exit` 就立刻返回，紧接着在同一端口重新监听，
   旧 server 还没释放 → 绑定失败。

4. 托盘里的 uvicorn 线程没有任何监督：线程死了托盘还活着，
   菜单能点、房间全没了。

用法: ./.venv/Scripts/python.exe scripts/check_tray_webui.py
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "scripts" / "tray_win.py").read_text(encoding="utf-8")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serving(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def main() -> int:
    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_tray_"))
    proj = tmp / "proj"
    (proj / "scripts").mkdir(parents=True)
    (proj / "data").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "tray_win.py", proj / "scripts" / "tray_win.py")
    (proj / "data" / "config.yaml").write_text(
        (ROOT / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    spec = importlib.util.spec_from_file_location(
        "tray_under_test", proj / "scripts" / "tray_win.py")
    tray = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tray)

    busy_sock = None
    try:
        ok("DATA_DIR 隔离到临时目录", "ayabot_tray_" in str(tray.DATA_DIR), tray.DATA_DIR)
        ok("配置路径是绝对路径",
           tray._RESOLVED_CONFIG_PATH.is_absolute()
           and str(tray.DATA_DIR) in str(tray._RESOLVED_CONFIG_PATH),
           tray._RESOLVED_CONFIG_PATH)

        # ── 1. 端口被占用必须报失败 ──
        print("[1] 端口被占用时 _start_webui 必须返回 False")
        busy_sock = socket.socket()
        busy_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy_sock.bind(("0.0.0.0", 0))
        busy_sock.listen(1)
        busy_port = busy_sock.getsockname()[1]
        tray._port = busy_port
        t0 = time.time()
        res = tray._start_webui(config_path=str(proj / "data" / "config.yaml"))
        elapsed = time.time() - t0
        ok("返回 False", res is False, res)
        err = getattr(tray._start_webui, "_last_error", "")
        ok("_last_error 提到端口占用", "端口" in err, err)
        ok("没有干等满超时", elapsed < 14, f"{elapsed:.1f}s")
        ok("给出可照做的提示", "设置端口" in tray._webui_failure_hint(err),
           tray._webui_failure_hint(err)[:60])
        ok("端口仍被占（不是我们起来的）", _serving(busy_port))

        # ── 2. 正常端口必须报成功且真的在监听 ──
        print("[2] 空闲端口：返回 True 且端口真的在监听")
        good_port = _free_port()
        tray._port = good_port
        res = tray._start_webui(config_path=str(proj / "data" / "config.yaml"))
        ok("返回 True", res is True, res)
        ok("端口在监听", _serving(good_port))
        ok("server.started=True",
           getattr(tray._start_webui._server, "started", None) is True)

        # ── 3. _restart_webui 不带参数也能起（绝对路径兜底）──
        print("[3] _restart_webui() 不传参数：仍然能起（不再依赖 cwd）")
        import os
        new_port = _free_port()
        tray._port = new_port
        cwd_before = os.getcwd()
        os.chdir(tmp)          # 一个没有 config.yaml 的目录，模拟 exe 从别处启动
        try:
            res = tray._restart_webui()
        finally:
            os.chdir(cwd_before)
        ok("返回 True", res is True, getattr(tray._start_webui, "_last_error", ""))
        ok("新端口在监听", _serving(new_port))

        # ── 4. _stop_webui 必须等旧线程退出（端口要释放）──
        print("[4] _stop_webui 同步等旧 server 释放")
        tray._stop_webui()
        ok("停止后端口已释放", not _serving(new_port))
        ok("标记为主动停止", tray._start_webui._intentional_stop is True)

        # ── 5. 看门狗：线程死了要重启；主动停止时不重启 ──
        print("[5] WebUI 看门狗")
        ok("存在看门狗函数", callable(getattr(tray, "_webui_watchdog", None)))

        class FakeServer:
            def __init__(self, started: bool, should_exit: bool = False) -> None:
                self.started = started
                self.should_exit = should_exit

        # 5a. 主动停止期间不重启
        tray._start_webui._intentional_stop = True
        tray._start_webui._server = FakeServer(False)
        calls: list[int] = []
        real_restart = tray._restart_webui
        tray._restart_webui = lambda *a, **k: calls.append(1) or True
        real_sleep = tray.time.sleep

        def fast_sleep(_s: float) -> None:
            real_sleep(0.001)

        tray.time.sleep = fast_sleep
        tray._stop_event.clear()
        th = threading.Thread(target=tray._webui_watchdog, daemon=True)
        th.start()
        time.sleep(0.4)
        tray._stop_event.set()
        th.join(timeout=5)
        tray.time.sleep = real_sleep
        ok("主动停止时不自动重启", calls == [], calls)

        # 5b. 非主动停止 + 线程已死 → 要重启
        tray._start_webui._intentional_stop = False
        tray._start_webui._server = FakeServer(False)
        calls.clear()
        tray._stop_event.clear()
        tray.time.sleep = fast_sleep
        th = threading.Thread(target=tray._webui_watchdog, daemon=True)
        th.start()
        time.sleep(0.4)
        tray._stop_event.set()
        th.join(timeout=5)
        tray.time.sleep = real_sleep
        tray._restart_webui = real_restart
        ok("检测到线程已死会重启", len(calls) >= 1, len(calls))

        # ── 6. port.txt 原子写 ──
        print("[6] 端口文件原子写")
        tray._save_port(23456)
        ok("写入正确", tray._read_port() == 23456, tray._read_port())
        ok("没有残留 .tmp", not list(tray.DATA_DIR.glob(".port.txt.tmp*")),
           [p.name for p in tray.DATA_DIR.glob("*tmp*")])
        bad = tray.DATA_DIR / "port.txt"
        bad.write_text("not-a-number", encoding="utf-8")
        ok("坏内容退回默认端口", tray._read_port() == tray._DEFAULT_PORT, tray._read_port())

        # ── 7. 结构断言 ──
        print("[7] 结构断言")
        ok("_start_webui 返回 bool", "def _start_webui(config_path: str | None = None) -> bool" in SRC)
        ok("不再用「提前 ready」的信号", 'q: _queue.Queue' not in SRC
           and "import queue as _queue" not in SRC)
        ok("等 server.started 才算成功", 'getattr(server, "started", False)' in SRC)
        ok("捕获 SystemExit（绑定失败）", "except SystemExit as exc:" in SRC)
        ok("保存旧的启动错误供弹窗使用", "_start_webui._last_error" in SRC)
        ok("_stop_webui 等线程退出", "for _ in range(80):" in SRC)
        ok("_restart_webui 返回 bool",
           "def _restart_webui(config_path: str | None = None) -> bool" in SRC)
        ok("重启走绝对路径", "_start_webui(config_path or str(_RESOLVED_CONFIG_PATH))" in SRC)
        ok("改端口失败会回退", "_port = old_port" in SRC)
        ok("main 启动失败会弹窗", "_webui_failure_hint(err)" in SRC)
        ok("main 挂了看门狗", "target=_webui_watchdog" in SRC)
        ok("退出时会先停 WebUI", "icon.stop()" in SRC and "_stop_webui()" in SRC)
    finally:
        if busy_sock is not None:
            busy_sock.close()
        try:
            tray._stop_event.set()
            tray._stop_webui()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("TRAY WEBUI OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
