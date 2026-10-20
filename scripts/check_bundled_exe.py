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

# 关键代码特征（累积：v1.2.0 的修复 + v1.2.1 的修复，防止老代码换版本号重发）
EXPECTED_MODULES = ("app.gift_v2",)
EXPECTED_STRINGS = {
    "app.bot": ("SEND_GIFT_V2", "record_guard_event", "USER_TOAST_MSG_V2",
                "bot_status.json", "_recent_guards"),
    "app.storage": ("guard_events", "record_guard_event"),
    "app.web.server": ("/api/update/check", "desired_states.json", "上舰记录", "魔搭",
                       "/guards",
                       # v1.2.1: 僵尸任务/无心跳恢复 + 期望状态迁移
                       "_bootstrap_desired_from_heartbeats", "疑似僵尸任务",
                       # v1.2.2: 更新检查提速（版本号抢答 / 缓存预热 / 代理取更新说明）
                       "_fetch_tag_race", "_warm_update_cache", "/api/update/changelog",
                       "_LANE_PRIORITY", "_probe_all",
                       # v1.2.3: 国内线路走 Gitee 发行版
                       "_fetch_gitee_tag", "_download_lanes", "_GITEE_RELEASES_API",
                       # v1.2.4: 用发行版列表按版本号挑最新（/releases/latest 是 latest 别名）
                       "_is_version_tag", "_VERSION_TAG_RE"),
    "app.auth": ("auth_status.json", "needs_login"),
    # v1.2.1: 自动清 MOTW（托盘启动时；入口脚本在 CArchive 顶层，不在 PYZ 里）
    "tray_win": ("Zone.Identifier", "_unblock_self"),
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
        consts: set[str] = set()
        if mod in names:
            code = pyz.extract(mod)
            walk_consts(code, consts)
        elif mod in arch.toc:
            # 入口脚本（tray_win.py）不在 PYZ 里，是 CArchive 顶层的 marshalled code
            import marshal

            raw = arch.extract(mod)
            if isinstance(raw, tuple):
                raw = raw[1]
            walk_consts(marshal.loads(raw), consts)
        else:
            failures.append(f"缺少模块 {mod}")
            print(f"  MISS 模块 {mod}")
            continue
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
