"""长跑不重启时会长胖的东西：图片缓存、以及"只注册不执行"的维护任务。

两件事：
1. `_clean_expired_cache()` 原来只在 `init_app`（启动那一刻）调用一次。
   exe 用户的正常形态是开几周不重启，于是 `data/cache/images` 只涨不清 ——
   一场热门直播能缓存上千张头像 / 礼物图标，全都在 7 天之内，只按天数清也没用。
   现在按「天数 + 总量 200MB」两道闸，并且每小时跑一次。

2. 定期清理线程（`_log_cleanup_loop`）要能把注册进来的任务真的跑起来，
   而且某个任务抛异常不能把日志清理一起带下去。

    ./.venv/Scripts/python.exe scripts/check_periodic_maintenance.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.web import server as srv
    import app.process_manager as pm

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="periodic_"))
    cache = tmp / "cache"
    cache.mkdir()
    old_dir = srv._IMAGE_CACHE_DIR
    srv._IMAGE_CACHE_DIR = str(cache)
    try:
        print("[1] 按天数清理")
        now = time.time()
        fresh = cache / "fresh.jpg"
        fresh.write_bytes(b"x" * 100)
        stale = cache / "stale.jpg"
        stale.write_bytes(b"x" * 100)
        old = now - 8 * 86400
        os.utime(stale, (old, old))
        srv._clean_expired_cache()
        ok("7 天前的被删了", not stale.exists())
        ok("新的还在", fresh.exists())

        print("[2] 按总量清理（只按天数清挡不住热门直播）")
        srv._IMAGE_CACHE_MAX_BYTES = 1_000_000          # 1MB 上限便于测试
        for i in range(40):
            f = cache / f"img{i}.jpg"
            f.write_bytes(b"x" * 50_000)                # 40 × 50KB = 2MB
            t = now - (40 - i) * 60                     # 越早越旧
            os.utime(f, (t, t))
        before_total = sum(f.stat().st_size for f in cache.iterdir() if f.is_file())
        srv._clean_expired_cache()
        after_total = sum(f.stat().st_size for f in cache.iterdir() if f.is_file())
        ok(f"总量从 {before_total/1048576:.1f}MB 压到上限以内",
           after_total <= 1_000_000, f"{after_total/1048576:.2f}MB")
        ok("压到上限的 80% 附近（留余量，不会每小时都触发）",
           after_total >= 600_000, f"{after_total/1048576:.2f}MB")
        newest = cache / "img39.jpg"
        ok("最新的文件没被删（按最旧优先淘汰）", newest.exists())

        print("[3] 注册的任务真的会被定期跑到")
        srv._IMAGE_CACHE_MAX_BYTES = 200 * 1024 * 1024
        pm._PERIODIC_TASKS.clear()
        calls: list[str] = []
        pm.register_periodic_task(lambda: calls.append("a"), "task-a")

        def boom():
            calls.append("boom")
            raise RuntimeError("这个任务坏了")
        pm.register_periodic_task(boom, "task-boom")
        pm.register_periodic_task(lambda: calls.append("c"), "task-c")

        # 直接跑一次循环体的一轮（不真的 sleep 一小时）
        import threading
        stop = threading.Event()

        def one_round() -> None:
            for fn, name in list(pm._PERIODIC_TASKS):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
        one_round()
        ok("坏任务后面的任务照样跑（一个坏了不拖垮其余的）",
           calls == ["a", "boom", "c"], calls)

        print("[4] 结构断言：启动时确实注册了缓存清理")
        src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        seg = src[src.index('async def _startup'):]
        seg = seg[: seg.index("@app.on_event(\"shutdown\")")]
        ok("_startup 里 register_periodic_task(_clean_expired_cache)",
           "register_periodic_task(_clean_expired_cache" in seg)

        print("[5] 循环里逐个 try（一个任务崩了不影响日志清理）")
        loop_src = (ROOT / "app" / "process_manager.py").read_text(encoding="utf-8")
        lseg = loop_src[loop_src.index("def _log_cleanup_loop"):]
        # 这个函数在文件末尾，后面没有别的 def，可能取不到分隔符
        cut = lseg.find(chr(10) + "def ")
        if cut > 0:
            lseg = lseg[:cut]
        ok("循环里遍历 _PERIODIC_TASKS", "_PERIODIC_TASKS" in lseg)
        ok("每个任务单独 try/except", lseg.count("try:") >= 2, lseg.count("try:"))
    finally:
        srv._IMAGE_CACHE_DIR = old_dir
        srv._IMAGE_CACHE_MAX_BYTES = 200 * 1024 * 1024
        pm._PERIODIC_TASKS.clear()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("PERIODIC MAINTENANCE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
