"""房间日志接口的三个坑：读整份文件、`lines` 不夹取、卡住事件循环。

`/api/rooms/{id}/log` 以前是：
    text = log_path.read_text()          # 整份读进内存（几十上百 MB）
    all_lines = text.splitlines()
    tail = all_lines[-lines:] ...        # lines 是外部输入，完全没夹
        ├─ lines=10**9  → 返回整个文件
        └─ lines=-5     → all_lines[5:]，返回除前 5 行以外的**全部**

而且这段同步 IO 跑在 async 端点里 —— 大日志会把事件循环卡住，
期间守护进程巡检（房间崩溃/假死自动重启）和其它所有接口一起停摆。

`/api/rooms/{id}/log/export` 同样 `read_text()` 后塞进 Response，
等于把日志复制一份到内存里。

    ./.venv/Scripts/python.exe scripts/check_log_endpoint.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 19013
ROOM = "1946287911"
TOKEN = "log-ep"


def main() -> int:
    import uvicorn
    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="logep_"))
    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    try:
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (tmp / "data").mkdir(parents=True)
        (tmp / "config.yaml").write_text(example, encoding="utf-8")
        room = tmp / "rooms" / ROOM
        (room / "data").mkdir(parents=True)
        (room / "config.yaml").write_text(example, encoding="utf-8")
        log_path = room / "bot.log"

        # 造一份 12MB / 12 万行的日志（远超 4MB 尾部预算）
        line = "2026-11-25 12:00:00,000 [INFO] bili-live-robot: " + "X" * 60
        with log_path.open("w", encoding="utf-8") as fh:
            for i in range(120_000):
                fh.write(f"{line} #{i}\n")
            fh.write("LAST_LINE_MARKER\n")
        size_mb = log_path.stat().st_size / 1048576
        print(f"  测试日志：{size_mb:.1f} MB / 120001 行")

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._API_TOKEN = TOKEN
        srv._SESSIONS[TOKEN] = (time.time() + 3600, "t", "admin", [])
        srv._rate_limited = lambda ip: False  # type: ignore[assignment]

        cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="critical")
        server = uvicorn.Server(cfg)
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(120):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)

        def get(path: str, timeout: float = 60.0) -> tuple[int, bytes]:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                         headers={"Cookie": f"session={TOKEN}"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()
            except Exception as exc:  # noqa: BLE001
                return 0, str(exc).encode()

        print("[1] 默认 200 行")
        code, body = get(f"/api/rooms/{ROOM}/log")
        data = json.loads(body) if code == 200 else {}
        lines = data.get("lines") or []
        ok("HTTP 200", code == 200, code)
        ok("返回 200 行", len(lines) == 200, len(lines))
        ok("最后一行是最新的", lines and lines[-1] == "LAST_LINE_MARKER", lines[-1] if lines else "-")

        print("[2] lines=10**9 必须夹取")
        code, body = get(f"/api/rooms/{ROOM}/log?lines=1000000000")
        n = len((json.loads(body).get("lines") or [])) if code == 200 else -1
        ok("不返回整个文件", n <= 5000, f"{n} 行")
        ok("至少返回了点东西", n > 0, n)

        print("[3] lines=-5 不能变成「除前 5 行以外全部」")
        code, body = get(f"/api/rooms/{ROOM}/log?lines=-5")
        n = len((json.loads(body).get("lines") or [])) if code == 200 else -1
        ok("负数按默认处理，不返回海量行", 0 < n <= 5000, f"{n} 行")

        print("[4] 读大文件不能卡住事件循环")
        # 并发打「轻量接口」，同时请求日志；轻量接口的延迟就是事件循环有没有被占住的证据
        lat: list[float] = []
        stop = threading.Event()

        def hammer() -> None:
            while not stop.is_set():
                t0 = time.perf_counter()
                get("/api/time", timeout=10)
                lat.append(time.perf_counter() - t0)
                time.sleep(0.01)

        th = threading.Thread(target=hammer, daemon=True)
        th.start()
        time.sleep(0.2)
        t0 = time.perf_counter()
        get(f"/api/rooms/{ROOM}/log?lines=5000")
        log_ms = (time.perf_counter() - t0) * 1000
        time.sleep(0.2)
        stop.set()
        th.join(timeout=5)
        worst = max(lat) * 1000 if lat else 0
        print(f"  日志请求 {log_ms:.0f} ms；期间 /api/time 最慢 {worst:.0f} ms（{len(lat)} 次）")
        ok("日志请求本身不慢（< 3000ms）", log_ms < 3000, f"{log_ms:.0f}ms")
        ok("**期间轻量接口没有被卡住**（< 800ms）", worst < 800, f"{worst:.0f}ms")

        print("[5] 导出走流式，不整份读进内存")
        t0 = time.perf_counter()
        code, body = get(f"/api/rooms/{ROOM}/log/export", timeout=120)
        exp_ms = (time.perf_counter() - t0) * 1000
        print(f"  导出 {len(body) / 1048576:.1f} MB / {exp_ms:.0f} ms")
        ok("导出成功", code == 200, code)
        ok("导出的确是整份（含最后一行）", b"LAST_LINE_MARKER" in body)
        src = (ROOT / "app/web/server.py").read_text(encoding="utf-8")
        seg = src[src.index('async def api_room_log_export'):]
        seg = seg[:seg.index("@app.post")]
        ok("用的是 FileResponse（流式）", "FileResponse" in seg)
        # 注意：注释里提到 read_text() 不算 —— 要查真正的调用
        ok("不再整份 read_text(log_path)", "log_path.read_text(" not in seg
           and 'str(log_path)' in seg)

        server.should_exit = True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("LOG ENDPOINT OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
