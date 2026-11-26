"""一个写坏的房间配置，不能把整个后台带下去。

`_list_rooms_from_disk()` 是 `/api/rooms` 的唯一数据源，而它解完 yaml 就直接
`cfg.get("web_ui", {}).get("bot_name", "")` ——
  - 文件里写 `web_ui:`（空值）→ yaml 给出 None → `.get` 抛 AttributeError
  - 文件里写成列表（粘错内容）→ cfg 是 list → 连 `.get` 都没有
于是**一个房间**的 config.yaml 写错，`/api/rooms` 500，整个后台白屏/报错，
连「把那个房间删掉」都进不去 —— 用户遇到的就是"一打开就报错"。

保存配置那条路同样：`raw.setdefault("web_ui", {})` 拿到的是已经写坏的值
（None / 字符串），接着 `web_ui["bot_name"] = ...` 直接 TypeError → 500。

    ./.venv/Scripts/python.exe scripts/check_room_list_robust.py
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

PORT = 19017
TOKEN = "roomrobust"

# 房间 id → 写进 config.yaml 的原文
BROKEN = {
    "1111111111": "web_ui:\nanchor_uid: 1111111111\n",              # web_ui 空值 → None
    "2222222222": "- 这是一个列表\n- 不是映射\n",                     # 顶层是 list
    "3333333333": "web_ui: \"字符串\"\nanchor_uid: 3333333333\n",     # web_ui 是字符串
    "4444444444": "web_ui: {bot_name: 正常房间}\nanchor_uid: 4444444444\nroom_display_id: 4444444444\n",
    "5555555555": "\t web_ui: {}\n",                                 # Tab 缩进，yaml 直接解不了
}


def main() -> int:
    import uvicorn
    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="roomrobust_"))
    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    try:
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (tmp / "data").mkdir(parents=True)
        (tmp / "config.yaml").write_text(example, encoding="utf-8")
        for rid, raw in BROKEN.items():
            d = tmp / "rooms" / rid
            (d / "data").mkdir(parents=True)
            (d / "config.yaml").write_text(raw, encoding="utf-8")

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

        def call(path: str, method="GET", body=None) -> tuple[int, str]:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}{path}", data=data, method=method,
                headers={"Cookie": f"session={TOKEN}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                return 0, str(exc)

        print("[1] /api/rooms 不能被写坏的配置打挂")
        code, body = call("/api/rooms")
        ok("HTTP 200", code == 200, f"{code} {body[:120]}")
        try:
            data = json.loads(body)
            rooms = data.get("rooms") if isinstance(data, dict) else data
            ids = {r["room_id"] for r in (rooms or [])}
        except Exception:  # noqa: BLE001
            ids = set()
        ok("坏配置的房间也都列出来了（还能点进去删）",
           set(BROKEN) <= ids, f"缺 {sorted(set(BROKEN) - ids)}")
        ok("正常房间没被牵连", "4444444444" in ids)

        print("[2] 房间配置不能读出 500")
        for rid in BROKEN:
            code, body = call(f"/api/rooms/{rid}/config")
            ok(f"{rid} GET config 不是 5xx", code < 500, f"{code} {body[:80]}")

        print("[3] 写坏配置的房间也要能保存（保存要顺手修好它）")
        for rid in ("1111111111", "2222222222", "3333333333"):
            code, body = call(f"/api/rooms/{rid}/config", "POST",
                              {"room_name": "改好了", "web_ui": {"bot_name": "bot"}})
            ok(f"{rid} 保存不是 5xx", code < 500, f"{code} {body[:100]}")

        print("[4] 保存要真的把 web_ui 修成字典（不是靠缓存糊过去）")
        import yaml as _yaml
        raw = _yaml.safe_load((tmp / "rooms" / "1111111111" / "config.yaml").read_text(encoding="utf-8"))
        ok("web_ui 现在是 dict", isinstance(raw.get("web_ui"), dict), type(raw.get("web_ui")).__name__)
        ok("同一次保存里的其它字段也写进去了",
           raw.get("room_name") == "改好了" and raw.get("web_ui", {}).get("bot_name") == "bot")
        ok("原本就有的字段没丢", str(raw.get("anchor_uid")) == "1111111111", raw.get("anchor_uid"))

        print("[5] 存完再读，房间列表照常")
        code, body = call("/api/rooms")
        ok("仍然 200", code == 200, code)

        server.should_exit = True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("ROOM LIST ROBUST OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
