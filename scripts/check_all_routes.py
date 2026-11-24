"""全路由冒烟：把每一条路由都以管理员身份打一遍，任何 5xx 都算失败。

目的：抓「平时没人点、一调就 500」的接口 —— 前端 e2e 和针对性 check 脚本
都覆盖不到它们，只有枚举路由表才能发现。

（这轮就是用它对出来的：`POST /api/rooms/{不存在的id}/start` 返回
500「启动失败，请检查配置」，而 `restart` 更糟 —— 200 + {"ok": false}，
前端 `if (res.ok)` 会当成重启成功。）

    ./.venv/Scripts/python.exe scripts/check_all_routes.py
"""
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path("C:/Users/shinshi/ayabot_dir/Ayabot")
sys.path.insert(0, str(ROOT))
import uvicorn  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from app.web import server as srv  # noqa: E402

TOKEN = "smoke-admin"
PORT = 18943
ROOM = "1946287911"
GHOST = "9000000001"          # 不存在的房间：DELETE 打它，不碰真数据
GHOST_ACC = "9000000001"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smoke_"))
    (tmp / "data").mkdir(parents=True)
    room = tmp / "rooms" / ROOM
    (room / "data").mkdir(parents=True)
    (room / "config.yaml").write_text(
        (ROOT / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    from app.storage import GiftEvent, StatsStore
    st = StatsStore(str(room / "data" / "bot.db"))
    now = int(time.time())
    st.record_gift_event(GiftEvent(
        ts=now, month=StatsStore.month_of_ts(now), uid=42, uname="甲",
        event_type="SEND_GIFT", gift_name="辣条", gift_num=2, is_blind_box=False,
        blind_box_cost=0, actual_value=200, profit_value=0,
        raw_json=json.dumps({"total_coin": 200, "giftName": "辣条", "num": 2})))
    st.record_danmaku(ts=now, uid=42, uname="甲", content="测试")
    st.close()

    # 根 config.yaml 必须真实存在：general_config / llm_config / api_token 这些
    # 保存类接口都要写它，缺了会一律 500 —— 那是测试环境问题，会淹没真 bug
    (tmp / "config.yaml").write_text(
        (ROOT / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    srv._ROOMS_BASE_DIR = str(tmp)
    srv._ROOM_DB_PATH_CACHE.clear()
    srv._DB_PATH = str(tmp / "data" / "bot.db")
    srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
    srv._API_TOKEN = TOKEN
    srv._SESSIONS[TOKEN] = (time.time() + 3600, "smoke", "admin", [])
    srv._rate_limited = lambda ip: False  # type: ignore[assignment]
    srv._GIFT_CACHE_BUILT = True
    srv._GIFT_PRICE_CACHE = {"辣条": (100, "silver")}

    cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="critical")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(120):
        if getattr(server, "started", False):
            break
        time.sleep(0.1)

    today = time.strftime("%Y-%m-%d")
    # 每条路由用什么参数/方法去打（只读优先，破坏性只打不存在的对象）
    def concrete(method: str, path: str) -> tuple[str, str, dict | None]:
        p = (path
             .replace("{room_id}", ROOM if method == "GET" else GHOST)
             .replace("{uid}", GHOST_ACC)
             .replace("{name}", "favicon.ico"))
        body = None
        if method in ("POST", "PUT", "PATCH"):
            body = {}
            lo = p.lower()
            if "login" in lo:
                body = {"username": "ayabot", "password": "123456"}
            elif "/start" in lo or "/stop" in lo or "/restart" in lo:
                body = {"room_id": GHOST}
            elif "delete_old" in lo:
                body = {"date": today, "type": "gift"}
            elif "password" in lo:
                body = {"old_password": "", "new_password": ""}
            elif "verify" in lo:
                body = {}
            elif "nickname" in lo:
                body = {"nickname": "x"}
            elif "api_token" in lo:
                body = {}
            elif "stats_repair" in lo:
                body = {}
            elif "danmaku/clear" in lo:
                body = {}
            elif "import/danmaku-xml" in lo:
                body = None
        # 查询串按接口补最小必需参数
        qs = []
        if "ranking" in p or "guards" in p or "export_all" in p:
            qs.append(f"rStart={today}&rEnd={today}")
        if "user_gifts" in p:
            qs.append(f"uid=42&date={today}")
        if "user_dates" in p:
            qs.append("uid=42")
        if "danmaku_log" in p:
            qs.append(f"room_id={ROOM}")
        if "/ranking" in p and "rooms" not in p:
            qs.append(f"start={today}&end={today}")
        if "/guards" in p and "rooms" not in p:
            qs.append(f"rStart={today}&rEnd={today}")
        if p.endswith("/api/update/check"):
            qs.append("force=0")
        if qs:
            p = p + ("&" if "?" in p else "?") + "&".join(qs)
        return method, p, body

    routes = []
    for r in srv.app.routes:
        if not isinstance(r, APIRoute):
            continue
        for m in sorted(r.methods):
            if m in ("HEAD", "OPTIONS"):
                continue
            routes.append((m, r.path))
    routes.sort(key=lambda x: (x[0], x[1]))

    print(f"共 {len(routes)} 条路由，逐条以管理员身份调用")
    print("-" * 78)
    bad: list[tuple[str, str, int, str]] = []
    for method, path in routes:
        m, p, body = concrete(method, path)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}{p}", data=data, method=m)
        req.add_header("Cookie", f"session={TOKEN}")
        req.add_header("Content-Type", "application/json")
        if p.endswith("import/danmaku-xml"):
            req.add_header("X-Filename", "probe.xml")
            req.data = b'<?xml version="1.0"?><i></i>'
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                code, text = resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            code, text = exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            code, text = 0, f"{type(exc).__name__}: {exc}"
        flag = ""
        if code >= 500 or code == 0:
            flag = "  <<< 500/异常"
            bad.append((method, p, code, text[:200]))
        print(f"  {code:3d}  {method:6s} {p[:70]:70s}{flag}")

    server.should_exit = True
    print("-" * 78)
    if bad:
        print(f"发现 {len(bad)} 个 500/异常：")
        for method, p, code, text in bad:
            print(f"\n  [{code}] {method} {p}")
            print(f"      {text[:300]}")
        shutil.rmtree(tmp, ignore_errors=True)
        return 1
    shutil.rmtree(tmp, ignore_errors=True)
    print("ROUTE SMOKE OK：没有任何接口返回 5xx")
    return 0


if __name__ == "__main__":
    sys.exit(main())
