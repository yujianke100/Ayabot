"""机器人「莫名其妙就没了」的时候，必须留下证据。

用户的原话是「有时候会意外关闭，点一下重启就恢复」。这种事后没有任何现场：
- 主线程的异常有 sys.excepthook，但**非主线程没有** —— 托盘线程、每个后台
  循环抛异常时默认只写 stderr，而窗口模式（console=False）下 stdout/stderr
  已经被接到 devnull，于是线程悄悄死掉、进程还活着，用户看到的就是
  「托盘图标还在，机器人不动了」；
- 硬崩溃（C 扩展访问违例、栈溢出）连异常钩子都走不到，进程直接消失，
  bot.log 里一个字都不会多。

这里用两个子进程真跑一遍：
  A. 线程里抛异常 → 进程要活着，bot.log 里要出现线程名和异常原文
  B. 硬崩溃（os.abort）→ data/crash.log 里要出现 Fatal Python error

    ./.venv/Scripts/python.exe scripts/check_crash_evidence.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

THREAD_CASE = textwrap.dedent('''
    import logging, sys, threading
    from pathlib import Path
    sys.path.insert(0, {root!r})
    from app.main import _install_exception_hooks

    data = Path(sys.argv[1])
    data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, filename=str(data / "bot.log"),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _install_exception_hooks(data)

    def boom():
        raise ValueError("线程里炸了（模拟托盘线程死掉）")

    t = threading.Thread(target=boom, name="tray-thread")
    t.start()
    t.join()
    print("STILL_ALIVE")
''')

ABORT_CASE = textwrap.dedent('''
    import logging, os, sys
    from pathlib import Path
    sys.path.insert(0, {root!r})
    from app.main import _install_exception_hooks

    data = Path(sys.argv[1])
    data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, filename=str(data / "bot.log"),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _install_exception_hooks(data)
    def deep():
        os.abort()
    deep()
''')


def main() -> int:
    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="crashev_"))
    try:
        print("[1] 非主线程抛异常")
        d1 = tmp / "case1"
        d1.mkdir()
        script = tmp / "thread_case.py"
        script.write_text(THREAD_CASE.format(root=str(ROOT)), encoding="utf-8")
        r1 = subprocess.run([sys.executable, str(script), str(d1)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=120)
        out = (r1.stdout or "") + (r1.stderr or "")
        ok("进程没有跟着死（线程异常不该带走主进程）", "STILL_ALIVE" in out, out.strip()[:80])
        log1 = (d1 / "bot.log").read_text(encoding="utf-8", errors="replace") if (d1 / "bot.log").exists() else ""
        ok("bot.log 里记下了线程名", "tray-thread" in log1)
        ok("bot.log 里记下了异常原文", "线程里炸了" in log1)
        ok("是按 CRITICAL 记的（不是被当成普通信息淹掉）", "CRITICAL" in log1)

        print("[2] 硬崩溃（os.abort，等价于 C 扩展炸掉）")
        d2 = tmp / "case2"
        d2.mkdir()
        script2 = tmp / "abort_case.py"
        script2.write_text(ABORT_CASE.format(root=str(ROOT)), encoding="utf-8")
        r2 = subprocess.run([sys.executable, str(script2), str(d2)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=120)
        crash = d2 / "crash.log"
        ok("进程确实异常结束了", r2.returncode != 0, f"rc={r2.returncode}")
        text = crash.read_text(encoding="utf-8", errors="replace") if crash.exists() else ""
        ok("写出了 data/crash.log", crash.exists())
        ok("里面有 Fatal Python error", "Fatal Python error" in text, text.strip().splitlines()[0][:60] if text.strip() else "空")
        ok("留下了最后一帧（函数名）", "deep" in text)

        print("[3] 崩溃转储不会无限长")
        big = d1 / "crash.log"
        big.write_text("X" * 1_100_000, encoding="utf-8")
        script3 = tmp / "again.py"
        script3.write_text(THREAD_CASE.format(root=str(ROOT)), encoding="utf-8")
        subprocess.run([sys.executable, str(script3), str(d1)], capture_output=True,
                       timeout=120)
        ok("超过 1MB 的老转储被截断", big.stat().st_size < 100_000, f"{big.stat().st_size} B")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("CRASH EVIDENCE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
