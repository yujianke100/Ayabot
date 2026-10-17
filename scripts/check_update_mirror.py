"""更新检查接口验证：能取到版本、能探测出可用下载镜像。

用法: ./.venv/Scripts/python.exe scripts/check_update_mirror.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 19897
TOKEN = "test-session-token"


def _get(path: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", headers={"Cookie": f"session={TOKEN}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def main() -> int:
    import uvicorn

    from app.config import load_config
    from app.web import server as srv

    cfg_path = str(ROOT / "rooms" / "1946287911" / "config.yaml")
    srv.init_app(load_config(cfg_path), config_path=cfg_path)
    srv._SESSIONS[TOKEN] = (time.time() + 600, "tester", "admin", [])

    server = uvicorn.Server(uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.5)
    if not server.started:
        print("FAILED: uvicorn 未启动")
        return 1

    failures: list[str] = []
    try:
        started = time.time()
        code, data = _get("/api/update/check")
        elapsed = time.time() - started
        print(f"HTTP {code} in {elapsed:.1f}s")
        print(json.dumps({k: v for k, v in data.items() if k != "changelog"}, ensure_ascii=False, indent=1)[:2200])

        if code != 200:
            failures.append(f"/api/update/check -> {code}")
        if not data.get("ok"):
            failures.append(f"未取到版本信息: {data.get('error')}")
        if not data.get("latest"):
            failures.append("缺少 latest 版本号")
        downloads = data.get("downloads") or []
        ok_ones = [d for d in downloads if d.get("ok")]
        print("\n可用线路:", [(d["name"], d.get("ms")) for d in ok_ones])
        if not ok_ones:
            failures.append("没有任何可用下载线路（含直连与镜像）")
        else:
            first = downloads[0]
            if not first.get("ok"):
                failures.append("recommended 未排在第一个可用线路")
        # 至少应探测到国内镜像（GitHub 直连在国内常不可用）
        if not any(d["name"] != "GitHub" and d.get("ok") for d in ok_ones):
            failures.append("国内镜像均不可用")
        if data.get("ok") and data.get("has_update") and not data.get("recommended"):
            failures.append("有待更新时未给出推荐下载地址")
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nUPDATE MIRROR OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
