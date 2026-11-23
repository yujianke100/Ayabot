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

        # ── 3. 日志轮转：旧实现在「行数不多但字节很大」时永远裁不掉 ──
        print("[3] 日志轮转（长行 / 超字节）")
        import logging

        h = tray._RotatingFileHandler(tray.DATA_DIR / "rot.log",
                                     max_lines=100, max_bytes=10_000)
        lg = logging.getLogger("rot_probe")
        lg.setLevel(logging.INFO)
        lg.handlers = [h]
        lg.propagate = False
        # 60 行 × 2KB ≈ 120KB，但行数（60）远小于 max_lines（100）。
        # 旧实现只在「行数 > max_lines」时裁剪 → 这 120KB 永远裁不掉。
        long_line = "y" * 2000
        for i in range(60):
            lg.info("%03d %s", i, long_line)
        rot = tray.DATA_DIR / "rot.log"
        size = rot.stat().st_size
        # 超限上界 = max_bytes + 检查周期 + 单行长度
        bound = 10_000 + h._check_bytes + 2500
        ok("长行大文件被裁到字节上限附近", size <= bound,
           f"{size}B (max_bytes=10000, 检查周期={h._check_bytes}B)")
        ok("确实裁过（远小于 60×2KB=120KB）", size < 60_000, f"{size}B")
        text = rot.read_text(encoding="utf-8")
        ok("裁剪后仍是完整 UTF-8（没有半行）", text.endswith("\n"))
        ok("保留的是最新的行", text.rstrip().splitlines()[-1].startswith("059"))

        # 再灌一批，验证不会无限增长
        for i in range(600):
            lg.info("%03d %s", i, long_line)
        size2 = rot.stat().st_size
        ok("继续写入也不会无限增长", size2 <= bound, f"{size2}B")

        # ── 4. 结构断言 ──
        print("[4] 结构断言")
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
        ok("日志轮转按字节判断", "_MAX_LOG_BYTES" in SRC and "_need_truncate" in SRC)
        ok("不再每次写入都读全文", "_check_bytes" in SRC and "self._written" in SRC)
        ok("轮转有锁", "self._lock = threading.RLock()" in SRC)
        ok("裁剪保留字节上限", "total > self._max_bytes" in SRC)
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
