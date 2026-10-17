"""校验 Release 里的 exe 确实包含本次修复的代码。

PyInstaller onefile 把 app 模块打包进 PYZ（zlib 压缩），纯文本 grep 搜不到
字符串常量（只能搜到模块名）。这里用 PyInstaller 自己的 reader 把 PYZ 解开，
再在 `app.bot` / `app.web.server` 的字节码常量里找关键字符串。

用法: ./.venv/Scripts/python.exe scripts/check_bundled_exe.py <path-to-exe>
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

# 本次 v1.2.0 应有的关键代码特征
EXPECTED_MODULES = ("app.gift_v2",)
EXPECTED_STRINGS = {
    "app.bot": ("SEND_GIFT_V2", "record_guard_event", "USER_TOAST_MSG_V2",
                "bot_status.json", "_recent_guards"),
    "app.storage": ("guard_events", "record_guard_event"),
    "app.web.server": ("/api/update/check", "desired_states.json", "上舰记录", "魔搭",
                       "/guards"),
    "app.auth": ("auth_status.json", "needs_login"),
}


def walk_consts(code, out: set[str]) -> None:
    """收集字节码里的字符串常量与属性/方法名（属性名在 co_names 里）。"""
    for const in code.co_consts:
        if isinstance(const, str):
            out.add(const)
        elif hasattr(const, "co_consts"):
            walk_consts(const, out)
    for name in getattr(code, "co_names", ()):
        if isinstance(name, str):
            out.add(name)


def main(exe_path: str) -> int:
    arch = CArchiveReader(exe_path)
    pyz_name = next((n for n in arch.toc if n.endswith(".pyz")), None)
    if pyz_name is None:
        print("FAILED: 找不到 PYZ 归档")
        return 1
    data = arch.extract(pyz_name)
    if isinstance(data, tuple):
        data = data[1]

    tmp = os.path.join(tempfile.gettempdir(), "ayabot_check.pyz")
    with open(tmp, "wb") as fh:
        fh.write(data)
    pyz = ZlibArchiveReader(tmp)
    names = set(pyz.toc)

    failures: list[str] = []
    print(f"PYZ 模块数: {len(names)}")
    for mod in EXPECTED_MODULES:
        ok = mod in names
        print(f"  {'OK ' if ok else 'MISS'} 模块 {mod}")
        if not ok:
            failures.append(f"缺少模块 {mod}")

    import marshal as _m  # noqa: F401

    for mod, needles in EXPECTED_STRINGS.items():
        if mod not in names:
            failures.append(f"缺少模块 {mod}")
            print(f"  MISS 模块 {mod}")
            continue
        code = pyz.extract(mod)
        consts: set[str] = set()
        walk_consts(code, consts)
        for needle in needles:
            ok = any(needle in c for c in consts)
            print(f"  {'OK ' if ok else 'MISS'} {mod} 含 {needle!r}")
            if not ok:
                failures.append(f"{mod} 缺少 {needle!r}")

    os.unlink(tmp)
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nBUNDLED EXE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "dist/Ayabot-latest.exe"))
