"""「新建直播间」不能因为找不到配置模板而失败。

覆盖三种运行布局：
  1) source — config 在 <root>/rooms/<id>/config.yaml（源码/开发运行）
  2) exe    — config 在 <data>/config.yaml（Windows 冻结版，%LOCALAPPDATA%\\Ayabot）
  3) bare   — 完全找不到任何模板（用户把根配置删了）→ 必须按内置默认值生成

历史上 3) 会直接返回 500「根目录 config.yaml 不存在」，用户看到的就是这句。
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

PORT = 19893
TOKEN = "test-session-token"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Cookie", f"session={TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except Exception:
            return exc.code, {"raw": raw[:300]}


def scenario(name: str, layout: str) -> bool:
    import uvicorn

    from app.config import load_config
    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_room_"))
    try:
        if layout == "source":
            room_dir = tmp / "rooms" / "111111"
            room_dir.mkdir(parents=True)
            cfg_path = room_dir / "config.yaml"
        else:
            cfg_path = tmp / "config.yaml"
        shutil.copy2(ROOT / "config.example.yaml", cfg_path)
        (tmp / "data").mkdir(exist_ok=True)

        srv.init_app(load_config(str(cfg_path)), config_path=str(cfg_path))
        srv._SESSIONS[TOKEN] = (time.time() + 600, "tester", "admin", [])

        patched = None
        if layout == "bare":
            # 模拟「任何地方都没有模板」：连仓库根的 config.example.yaml 也让它看不见
            import app.config as cfgmod
            patched = cfgmod.find_template_config
            cfgmod.find_template_config = lambda *_a, **_k: None

        print(f"\n=== {name}（布局 {layout}） ===")
        print("  _ROOMS_BASE_DIR =", srv._ROOMS_BASE_DIR)

        server = uvicorn.Server(uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(60):
            if server.started:
                break
            time.sleep(0.2)

        code, body = call("POST", "/api/rooms", {
            "anchor_uid": 672328094, "room_display_id": 21452505, "room_name": "测试",
        })
        print(f"  POST /api/rooms -> HTTP {code}: {str(body)[:160]}")

        room_cfg = Path(srv._ROOMS_BASE_DIR) / "rooms" / "21452505" / "config.yaml"
        exists = room_cfg.exists()
        print("  房间配置已生成:", exists)
        ok = code == 200 and exists
        if ok:
            # 生成的配置必须能被正常解析，且带上了房间号/主播 UID
            parsed = load_config(str(room_cfg))
            print("  解析结果: room_display_id =", parsed.room_display_id,
                  "anchor_uid =", parsed.anchor_uid)
            ok = bool(parsed.room_display_id) and bool(parsed.anchor_uid)

        server.should_exit = True
        time.sleep(0.6)
        return ok

    finally:
        if layout == "bare":
            import app.config as cfgmod
            cfgmod.find_template_config = patched
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    results = {
        "源码布局（ rooms/<id>/config.yaml ）": scenario("源码布局", "source"),
        "exe 布局（ <data>/config.yaml ）": scenario("exe 布局", "exe"),
        "无任何模板（ 内置默认值兜底 ）": scenario("无模板兜底", "bare"),
    }
    print("\n结果:")
    for k, v in results.items():
        print(f"  {'OK  ' if v else 'FAIL'} {k}")
    ok = all(results.values())
    print("\n" + ("CREATE ROOM OK" if ok else "CREATE ROOM FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
