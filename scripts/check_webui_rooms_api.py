"""WebUI 接口验证：登录态下检查 /api/rooms 是否带出凭证/心跳/崩溃状态。

不依赖 httpx/TestClient：在同进程起 uvicorn（测试端口），用 urllib + 注入的
session cookie 请求，验证完即关。

用法: ./.venv/Scripts/python.exe scripts/check_webui_rooms_api.py
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

PORT = 19898
TOKEN = "test-session-token"


def _get(path: str) -> tuple[int, str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", headers={"Cookie": f"session={TOKEN}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    import uvicorn

    from app.config import load_config
    from app.web import server as srv

    cfg_path = str(ROOT / "rooms" / "1946287911" / "config.yaml")
    config = load_config(cfg_path)
    srv.init_app(config, config_path=cfg_path)
    srv._SESSIONS[TOKEN] = (time.time() + 600, "tester", "admin", [])

    room_id = "1946287911"
    data_dir = Path(srv._ROOMS_BASE_DIR) / "rooms" / room_id / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    names = ("auth_status.json", "bot_status.json", "crash_info.json")
    paths = {n: data_dir / n for n in names}
    backups = {p: (p.read_bytes() if p.exists() else None) for p in paths.values()}

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
        paths["auth_status.json"].write_text(
            json.dumps({"status": "needs_login", "reason": "Cookie 已过期"}), encoding="utf-8")
        paths["bot_status.json"].write_text(
            json.dumps({"ts": int(time.time()) - 5, "live": True, "danmaku_connected": True}),
            encoding="utf-8")
        paths["crash_info.json"].write_text(
            json.dumps({"ts": int(time.time()) - 60, "reason": "进程已退出", "exit_code": 1}),
            encoding="utf-8")

        code, body = _get("/api/rooms")
        if code != 200:
            failures.append(f"/api/rooms -> {code} {body[:120]}")
            rooms = []
        else:
            rooms = json.loads(body).get("rooms", [])
        target = next((r for r in rooms if r["room_id"] == room_id), None)
        if target is None:
            failures.append("房间未出现在 /api/rooms")
        else:
            print("auth_status:", target.get("auth_status"), "| reason:", target.get("auth_reason"))
            print("heartbeat_age:", target.get("heartbeat_age"), "| recent_crash:", target.get("recent_crash"))
            print("heartbeat:", json.dumps(target.get("heartbeat"), ensure_ascii=False))
            if target.get("auth_status") != "needs_login":
                failures.append("auth_status 未透出")
            if target.get("heartbeat_age") is None:
                failures.append("heartbeat_age 未透出")
            if not target.get("recent_crash"):
                failures.append("recent_crash 未透出")

        code, html = _get("/")
        if code != 200:
            failures.append(f"/ -> {code}")
        for needle in ("goRelogin", "登录凭证已失效", "刚发生异常退出", "recent_crash",
                       "api/update/check", "魔搭 ModelScope", "ModelScope".lower()):
            if needle not in html:
                failures.append(f"首页缺少前端片段: {needle}")
        for needle in ("api-inference.modelscope.cn/v1", "2000 次免费 API 调用"):
            if needle not in html:
                failures.append(f"魔搭提示缺少内容: {needle}")
        print("page bytes:", len(html))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        for p, content in backups.items():
            if content is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(content)

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nWEBUI API OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
