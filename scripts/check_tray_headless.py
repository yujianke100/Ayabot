"""探针/自检：托盘不可用时，整个进程会不会跟着退出（第二十四轮）。

这是「机器人莫名其妙关闭」最直接的一条路径：
`_create_tray_icon()` 里 `import pystray` 失败（或者 `icon.run()` 抛异常）时，
以前是 `return` / 异常冒泡 → `main()` 结束 → 进程退出 →
后台 uvicorn 线程（daemon）和所有房间机器人一起消失。
--windowed 模式没有控制台，用户看到的就是「双击图标闪一下就没了」或「用着用着程序自己关了」。

用真实模块验证（复制到临时目录导入，DATA_DIR 隔离）：
  - 让 `import pystray` 必然失败（sys.modules['pystray'] = None）
  - 调 `main()`：修好后它不应该返回（要停在 `_stop_event.wait()`）
  - 期间 WebUI 必须仍在监听

用法: ./.venv/Scripts/python.exe scripts/check_tray_headless.py
"""
from __future__ import annotations

import importlib.util
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

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_headless_"))
    proj = tmp / "proj"
    (proj / "scripts").mkdir(parents=True)
    (proj / "data").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "tray_win.py", proj / "scripts" / "tray_win.py")

    port = _free_port()
    (proj / "data" / "config.yaml").write_text(
        (ROOT / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (proj / "data" / "port.txt").write_text(str(port), encoding="utf-8")

    spec = importlib.util.spec_from_file_location(
        "tray_headless_under_test", proj / "scripts" / "tray_win.py")
    tray = importlib.util.module_from_spec(spec)

    # 让托盘依赖不可用 + 不弹窗 + 不打开浏览器
    sys.modules["pystray"] = None  # type: ignore[assignment]
    spec.loader.exec_module(tray)
    tray._show_message = lambda *a, **k: None  # type: ignore[assignment]
    tray._open_webui = lambda: None  # type: ignore[assignment]
    tray._prompt_port = lambda: None  # type: ignore[assignment]
    tray._acquire_lock = lambda: True  # type: ignore[assignment]

    try:
        print("[1] 托盘依赖缺失时的行为")
        returned = {"v": False}

        def run_main() -> None:
            try:
                tray.main()
            finally:
                returned["v"] = True

        th = threading.Thread(target=run_main, daemon=True)
        th.start()
        th.join(timeout=25)

        ok("main() 没有立刻返回（进程不会自己退出）", not returned["v"],
           "main 已返回 = 真机上进程此时就结束了")
        ok("WebUI 仍在监听", _serving(port), f"port={port}")
        ok("退出标记尚未置位", not tray._stop_event.is_set())

        # 让它退出，避免测试进程挂住
        tray._stop_event.set()
        th.join(timeout=20)

        # ── 2. 单实例：已有实例时应打开它，而不是静默退出 ──
        print("[2] 重复启动（已有实例）")
        opened: list[str] = []
        tray._acquire_lock = lambda: False  # type: ignore[assignment]
        import webbrowser as _wb
        real_open = _wb.open
        _wb.open = lambda url, *a, **k: opened.append(url) or True  # type: ignore[assignment]
        try:
            tray.main()
        finally:
            _wb.open = real_open  # type: ignore[assignment]
        ok("打开了已有实例的后台", any(str(port) in u for u in opened), opened)

        # ── 3. 结构断言 ──
        print("[3] 结构断言")
        ok("_create_tray_icon 返回 bool",
           "def _create_tray_icon() -> bool" in SRC)
        ok("依赖缺失返回 False 而不是裸 return",
           'logger.error("pystray or Pillow not installed' in SRC
           and "return False" in SRC.split("def _create_tray_icon() -> bool")[1][:900])
        ok("main 用 try 包住托盘创建", "tray_ok = _create_tray_icon()" in SRC)
        ok("托盘不可用时停住主线程", "_stop_event.wait()" in SRC)
        ok("告诉用户后台地址", "后台服务正常运行" in SRC)
        ok("重复启动会打开已有实例", "已经在运行" in SRC and "webbrowser.open" in SRC)
        ok("嵌入式配置用原子写", "atomic_write_text(dst" in SRC)
    finally:
        tray._stop_event.set()
        try:
            tray._stop_webui()
        except Exception:  # noqa: BLE001
            pass
        sys.modules.pop("pystray", None)
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("TRAY HEADLESS OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
