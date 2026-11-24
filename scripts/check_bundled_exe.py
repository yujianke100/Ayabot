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
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

# 关键代码特征（累积：v1.2.0 的修复 + v1.2.1 的修复，防止老代码换版本号重发）
EXPECTED_MODULES = ("app.gift_v2", "app.tzutil")
EXPECTED_STRINGS = {
    "app.bot": ("SEND_GIFT_V2", "record_guard_event", "USER_TOAST_MSG_V2",
                "_recent_guards",
                # 第 25 轮：礼物「收到但没记上」的分项计数 + V2 批量序号
                "_gift_seen", "gift_dropped_decode", "_v2_index",
                "_gift_decode_sample",
                # 第 27 轮：事件类型计数 + 未识别礼物类事件 + 汇总日志
                "KNOWN_GIFT_EVENTS", "_bump_event_count",
                "_looks_like_gift_event", "_log_event_summary",
                "unknown_gift_events"),
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
                       "_is_version_tag", "_VERSION_TAG_RE",
                       # v1.2.5: AI 页的魔搭一键填入 + 新建房间走新的模板查找
                       "applyModelScope", "find_template_config", "write_default_config",
                       # v1.2.6: 前端库改为本地内置（CDN 挂了也不至于整页 {{ }} 裸模板）
                       "/vendor/", "_vendor_path", "vendor_asset", "bootfail",
                       # v1.2.7: 机器人配置模板带全同级字段（签文 / 关键词规则 / 冷却限流）
                       "AyabotBotTpl", "__ayabot_bot_tpl", "botTplSummaryText",
                       # v1.2.8: 录播 XML 导入 + 全局时区口径
                       "danmaku-xml", "ayabot_date", "_day_bounds", "/api/time",
                       # 第 19 轮：房间级权限（默认拒绝）+ 免登录白名单收敛 + 进程内状态清理
                       "_USER_ROOM_MUTATIONS", "_PUBLIC_EXTERNAL_GETS",
                       "_purge_stale_state", "ACL deny",
                       # 第 20 轮：非 admin 一律收紧、密钥接口、图片代理加固
                       "_SECRET_PATHS", "_redact_llm_key", "_check_proxy_image_url",
                       "_ALLOWED_IMAGE_SUFFIXES", "_GIFT_CACHE_RETRY_AT",
                       # 第 21 轮：日志/更新日志转义，登录限速，请求体上限
                       "escapeHtml", "_login_limited", "_MAX_REQUEST_BYTES",
                       "_USERS_CORRUPT",
                       # 第 22 轮：房间号 / uid 白名单 + 目录穿越兜底
                       "_valid_ident", "_safe_subdir", "_IDENT_RE",
                       "_ACCOUNT_PATH_RE",
                       # 第 23 轮：等扫码时不被当僵尸 + 保存失败要 5xx
                       "_room_waiting_login", "配置保存失败",
                       # 第 25 轮：礼物统计记不上时的界面提示
                       "gift_issue", "gift_dropped_no_uname"),
    "app.auth": ("auth_status.json", "needs_login",
                 # 第 23 轮：等扫码期间写心跳，守护进程据此不重启
                 "_write_waiting_heartbeat", "waiting_login"),
    # v1.2.5: 新建房间不再依赖根目录模板（多路径查找 + 内置默认值兜底）
    # 第 23 轮：心跳文件名提成共享常量（bot/auth/WebUI 三处共用一个）
    "app.config": ("find_template_config", "write_default_config",
                   "bot_status.json", "HEARTBEAT_FILE"),
    # v1.2.1: 自动清 MOTW（托盘启动时；入口脚本在 CArchive 顶层，不在 PYZ 里）
    "tray_win": ("Zone.Identifier", "_unblock_self",
                 # 第 24 轮：启动失败要能报出来 + WebUI 看门狗 + 绝对路径兜底
                 "_webui_watchdog", "_webui_failure_hint", "_RESOLVED_CONFIG_PATH",
                 "_port_serving", "_last_error"),
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

    # v1.2.6: 前端库必须真的打进 exe —— 缺了用户打开网页只会看到「界面资源没能加载出来」
    # PyInstaller 的 TOC 键在 Windows 上可能是反斜杠，统一成正斜杠再比。
    toc_norm = {k.replace("\\", "/") for k in arch.toc}
    for asset in ("app/web/vendor/vue.global.prod.js",
                  "app/web/vendor/chart.umd.min.js",
                  "app/web/vendor/tailwind.js",
                  "app/web/vendor/modern-screenshot.js"):
        ok = asset in toc_norm
        print(f"  {'OK ' if ok else 'MISS'} 资源 {asset}")
        if not ok:
            failures.append(f"缺少资源 {asset}")
    # 时区数据必须打进去：Windows 没有系统 tzdb，缺了 tzdata 的 .zi 文件，
    # 冻结版会静默退回系统本地时区（统计/删除全部错位），而且界面上完全看不出来。
    tz_files = [k for k in arch.toc if "tzdata" in k.replace("\\", "/")]
    ok = len(tz_files) > 0
    print(f"  {'OK ' if ok else 'MISS'} tzdata 数据文件（{len(tz_files)} 个）")
    if not ok:
        failures.append("exe 里没有 tzdata（ZoneInfo 会失效）")
    else:
        has_sh = any(k.replace("\\", "/").endswith("Asia/Shanghai") for k in arch.toc)
        print(f"  {'OK ' if has_sh else 'MISS'} 含 Asia/Shanghai 时区文件")
        if not has_sh:
            failures.append("tzdata 里缺 Asia/Shanghai")

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


def _default_exe() -> str:
    """不给参数时自己找最新的构建产物。

    以前默认写死 `dist/Ayabot-latest.exe`，而本地构建出来的是
    `dist/Ayabot-v<version>.exe` —— 直接跑就报「找不到文件」，看起来像测试坏了。
    """
    root = Path(__file__).resolve().parent.parent
    latest = root / "dist" / "Ayabot-latest.exe"
    if latest.exists():
        return str(latest)
    built = sorted((root / "dist").glob("Ayabot-v*.exe"))
    return str(built[-1]) if built else "dist/Ayabot-latest.exe"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else _default_exe()))
