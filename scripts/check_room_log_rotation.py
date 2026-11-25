"""房间级 bot.log 的轮转：字节上限 + 只读尾部 + 父进程不泄漏句柄。

和托盘日志（第 26 轮修过）是同一个教训，但房间日志这条路径一直没修：
- 只按行数截断 → 一行很长（整段 traceback / 单行 JSON）时行数很少却几十 MB，
  文件永远不缩；
- 每次启动 `start_room()` 都 `read_text()` 整份读进内存 → 大日志让 WebUI 卡住；
- `log_fp = log_path.open("a")` 传给了子进程，**父进程从不关闭** →
  每重启一次泄漏一个句柄，Windows 上还会让「删除房间」的 rmtree 撞 WinError 32。

    ./.venv/Scripts/python.exe scripts/check_room_log_rotation.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app import process_manager as pm

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="logrot_"))
    try:
        print("[1] 行数超限：保留最后 N 行")
        p = tmp / "many_lines.log"
        p.write_text("\n".join(f"line {i}" for i in range(2000)) + "\n", encoding="utf-8")
        ok("函数返回 True", pm.truncate_log_file(p, max_lines=100, max_bytes=10 ** 7))
        lines = p.read_text(encoding="utf-8").splitlines()
        ok("截到最后 100 行", len(lines) == 100, f"{len(lines)} 行")
        ok("保留的是最新的", lines[-1] == "line 1999", lines[-1])

        print("[2] 行数没超但字节超限：必须按字节裁")
        p2 = tmp / "huge_line.log"
        big = "X" * (2 * 1024 * 1024)     # 单行 2MB，只有 3 行
        p2.write_text("\n".join([big, big, big]) + "\n", encoding="utf-8")
        before = p2.stat().st_size
        ok("函数返回 True", pm.truncate_log_file(p2, max_lines=5000, max_bytes=1024 * 1024))
        after = p2.stat().st_size
        ok("**字节数被压到上限内**（只按行数永远做不到）", after <= 1024 * 1024,
           f"{before:,} B -> {after:,} B")

        print("[3] 大文件只读尾部（不整份读进内存）")
        p3 = tmp / "big_tail.log"
        with p3.open("w", encoding="utf-8") as fh:
            for i in range(30000):
                fh.write(f"padding line {i} " + "Y" * 200 + "\n")
            fh.write("THE_LAST_LINE\n")
        src = (ROOT / "app/process_manager.py").read_text(encoding="utf-8")
        body = src[src.index("def truncate_log_file"):]
        body = body[:body.index("\ndef ")]
        ok("大文件分支用 seek 读尾部", "fh.seek(size - budget)" in body)
        ok("没有无条件 read_text 整个文件",
           "if size <= max_bytes:" in body and "path.read_text" in body)
        before3 = p3.stat().st_size
        pm.truncate_log_file(p3, max_lines=50, max_bytes=512 * 1024)
        after3 = p3.stat().st_size
        ok("大文件被压小", after3 < before3, f"{before3:,} -> {after3:,}")
        tail = p3.read_text(encoding="utf-8").splitlines()
        ok("最后一行还在", tail and tail[-1] == "THE_LAST_LINE", tail[-1] if tail else "(空)")

        print("[4] 空文件 / 不存在的文件不报错")
        empty = tmp / "empty.log"
        empty.write_bytes(b"")
        ok("空文件返回 True", pm.truncate_log_file(empty) is True)
        ok("不存在的文件返回 True", pm.truncate_log_file(tmp / "nope.log") is True)

        print("[5] 父进程必须关闭 stdout 句柄（否则泄漏 + 删房间失败）")
        i_close = src.find("log_fp.close()")
        i_popen = src.find("stdout=log_fp")
        i_pid = src.find('_pidfile(room_id).write_text(str(proc.pid)')
        ok("log_fp.close() 存在", i_close > 0)
        ok("在 Popen 之后关闭", i_close > i_popen, f"popen={i_popen} close={i_close}")
        ok("在记录 PID 之前关闭", i_close < i_pid, f"close={i_close} pid={i_pid}")

        print("[6] 常量")
        ok("字节上限存在且合理（1MB – 64MB）",
           1024 * 1024 <= pm._MAX_LOG_BYTES <= 64 * 1024 * 1024,
           f"{pm._MAX_LOG_BYTES // 1024 // 1024} MB")
        ok("行数上限仍是 5000", pm._MAX_LOG_LINES == 5000)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("ROOM LOG ROTATION OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
