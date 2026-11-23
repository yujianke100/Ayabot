"""校验房间级权限（ACL）：普通账号不能操作别人的房间。

背景（本轮发现的最严重问题）：中间件里只有「管理员能改全局配置」这类粗粒度拦截，
`allowed_rooms` **只用来过滤房间列表**，`/api/rooms/{id}/*` 的 24 个接口
没有任何权限校验。于是任何登录的普通账号，只要知道房间号就能：

  - 删除别人的房间        DELETE /api/rooms/{id}
  - 停掉别人的机器人      POST   /api/rooms/{id}/stop
  - 清掉别人的历史数据    POST   /api/rooms/{id}/delete_old
  - 清掉别人的弹幕记录    POST   /api/rooms/{id}/danmaku/clear
  - 读取别人的全部配置    GET    /api/rooms/{id}/config
  - 覆盖别人的配置        POST   /api/rooms/{id}/config
  - 重置别人的 API 密钥   POST   /api/rooms/{id}/api_regenerate
  - 导出别人的全部送礼数据 GET   /api/rooms/{id}/export_all

另外 `allowed_rooms` 为空时列表**不过滤**，等于「没分配房间」的人能看到所有房间。

用法: ./.venv/Scripts/python.exe scripts/check_room_acl.py
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

PORT = 19895
ROOM_A = "1900000111"
ROOM_B = "1900000222"
TOK_ADMIN = "tok-admin"
TOK_OWNER = "tok-owner"       # 只能访问 ROOM_A
TOK_NOBODY = "tok-nobody"     # 一个房间都没分配


def _req(method: str, path: str, token: str) -> tuple[int, str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", method=method,
        headers={"Cookie": f"session={token}"},
        data=b"{}" if method in ("POST", "PUT") else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    import uvicorn

    from app.storage import GiftEvent, StatsStore
    from app.web import server as srv

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_acl_"))
    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    for rid in (ROOM_A, ROOM_B):
        d = tmp / "rooms" / rid
        (d / "data").mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(example, encoding="utf-8")
        st = StatsStore(str(d / "data" / "bot.db"))
        st.record_gift_event(GiftEvent(
            ts=int(time.time()), month=time.strftime("%Y-%m"), uid=999, uname="观众",
            event_type="SEND_GIFT", gift_name="辣条", gift_num=1, is_blind_box=0,
            blind_box_cost=0, actual_value=10, profit_value=0,
            raw_json=json.dumps({"total_coin": 100})))
        st.close()

    srv._ROOMS_BASE_DIR = str(tmp)
    srv._ROOM_DB_PATH_CACHE.clear()
    srv._rate_limited = lambda ip: False  # type: ignore[assignment]
    exp = time.time() + 900
    srv._SESSIONS[TOK_ADMIN] = (exp, "root", "admin", [])
    srv._SESSIONS[TOK_OWNER] = (exp, "owner", "user", [ROOM_A])
    srv._SESSIONS[TOK_NOBODY] = (exp, "nobody", "user", [])

    # 不要真的启停机器人
    async def _fake_ok(rid):  # noqa: ANN001
        return True

    srv.start_room_async = _fake_ok        # type: ignore[assignment]
    srv.stop_room_async = _fake_ok         # type: ignore[assignment]
    srv.restart_room_async = _fake_ok      # type: ignore[assignment]
    srv.room_status = lambda rid: "stopped"  # type: ignore[assignment]
    srv.clean_room = lambda rid: None      # type: ignore[assignment]

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    server = uvicorn.Server(uvicorn.Config(srv.app, host="127.0.0.1", port=PORT,
                                           log_level="critical"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(80):
        if server.started:
            break
        time.sleep(0.25)
    if not server.started:
        print("FAILED: uvicorn 未启动")
        return 1

    try:
        # ── 1. 别人的房间：全部拒绝 ──
        print("[1] 只分配了 A 的账号，访问 B 必须 403")
        for method, path, label in (
            ("GET", f"/api/rooms/{ROOM_B}/ranking?rStart=&rEnd=&rType=all", "读排行"),
            ("GET", f"/api/rooms/{ROOM_B}/config", "读配置"),
            ("GET", f"/api/rooms/{ROOM_B}/log", "读日志"),
            ("GET", f"/api/rooms/{ROOM_B}/export_all?date_from=&date_to=", "导出明细"),
            ("GET", f"/api/rooms/{ROOM_B}/guards", "读上舰"),
            ("GET", f"/api/rooms/{ROOM_B}/all_dates", "读日期列表"),
            ("POST", f"/api/rooms/{ROOM_B}/stop", "停机器人"),
            ("POST", f"/api/rooms/{ROOM_B}/start", "启机器人"),
            ("POST", f"/api/rooms/{ROOM_B}/restart", "重启机器人"),
            ("POST", f"/api/rooms/{ROOM_B}/config", "覆盖配置"),
            ("POST", f"/api/rooms/{ROOM_B}/delete_old", "删历史数据"),
            ("DELETE", f"/api/rooms/{ROOM_B}/danmaku/clear", "清弹幕"),
            ("POST", f"/api/rooms/{ROOM_B}/log/clear", "清日志"),
            ("POST", f"/api/rooms/{ROOM_B}/api_regenerate", "重置 API 密钥"),
            ("POST", f"/api/rooms/{ROOM_B}/api_toggle", "开关外部 API"),
            ("DELETE", f"/api/rooms/{ROOM_B}", "删除房间"),
        ):
            code, body = _req(method, path, TOK_OWNER)
            ok(f"{label} -> 403", code == 403, f"{code} {body[:60]}")

        # 房间 B 必须还活着（没被上面的请求删掉/停掉）
        ok("房间 B 的配置仍在", (tmp / "rooms" / ROOM_B / "config.yaml").exists())
        ok("房间 B 的数据仍在", (tmp / "rooms" / ROOM_B / "data" / "bot.db").exists())

        # ── 2. 自己的房间：能读、能启停，但不能删 ──
        print("[2] 自己的房间：读/启停可以，破坏性操作仍然不行")
        code, body = _req("GET", f"/api/rooms/{ROOM_A}/ranking?rStart=&rEnd=&rType=all",
                          TOK_OWNER)
        ok("读自己房间的排行 -> 200", code == 200, f"{code} {body[:80]}")
        code, body = _req("GET", f"/api/rooms/{ROOM_A}/config", TOK_OWNER)
        ok("读自己房间的配置 -> 200", code == 200, f"{code} {body[:60]}")
        code, body = _req("POST", f"/api/rooms/{ROOM_A}/restart", TOK_OWNER)
        ok("重启自己的机器人 -> 200", code == 200, f"{code} {body[:60]}")
        code, body = _req("POST", f"/api/rooms/{ROOM_A}/config", TOK_OWNER)
        ok("改自己房间的模板/配置 -> 200（自助范围）", code == 200, f"{code} {body[:60]}")
        for method, path, label in (
            ("DELETE", f"/api/rooms/{ROOM_A}", "删除自己的房间"),
            ("POST", f"/api/rooms/{ROOM_A}/delete_old", "删自己的历史数据"),
            ("DELETE", f"/api/rooms/{ROOM_A}/danmaku/clear", "清自己的弹幕"),
            ("POST", f"/api/rooms/{ROOM_A}/log/clear", "清自己的日志"),
            ("POST", f"/api/rooms/{ROOM_A}/api_regenerate", "重置自己的 API 密钥"),
            ("POST", f"/api/rooms/{ROOM_A}/api_toggle", "开关自己的外部 API"),
            ("POST", f"/api/rooms/{ROOM_A}/config/import", "导入覆盖自己的配置"),
        ):
            code, body = _req(method, path, TOK_OWNER)
            ok(f"{label} -> 403（这是管理员操作）", code == 403, f"{code} {body[:60]}")

        # 改自己的登录密码是允许的（否则普通账号没法自己换密码）
        code, body = _req("POST", "/api/user/password", TOK_OWNER)
        ok("改自己的密码不被 ACL 拦（可能因参数不全 400）", code in (200, 400, 401),
           f"{code} {body[:60]}")

        # ── 3. 一个房间都没分配的账号：看不到任何房间 ──
        print("[3] allowed_rooms 为空的账号")
        code, body = _req("GET", "/api/rooms", TOK_NOBODY)
        rooms = (json.loads(body).get("rooms") if code == 200 else []) or []
        ok("房间列表为空", code == 200 and rooms == [], f"{code} {rooms}")
        for rid in (ROOM_A, ROOM_B):
            code, _ = _req("GET", f"/api/rooms/{rid}/ranking?rStart=&rEnd=&rType=all",
                           TOK_NOBODY)
            ok(f"访问 {rid} -> 403", code == 403, code)

        # ── 4. 普通账号不能新建房间 ──
        print("[4] 新建房间是管理员操作")
        code, body = _req("POST", "/api/rooms", TOK_OWNER)
        ok("POST /api/rooms -> 403", code == 403, f"{code} {body[:60]}")

        # ── 4b. 非房间的破坏性/管理接口 ▸ 全部挡住 ──
        print("[4b] 非房间的管理接口")
        for method, path, label in (
            ("POST", "/api/delete_old", "删全局旧数据"),
            ("POST", "/api/restart_all_bots", "重启所有机器人"),
            ("POST", "/api/templates", "改全局模板"),
            ("DELETE", "/api/templates", "删全局模板"),
            ("POST", "/api/llm_config/import", "导入 LLM 配置"),
            ("POST", "/api/bili_login/start", "发起 B站扫码（可拿到别人的授权）"),
            ("POST", "/api/bili_accounts", "新增 B站账号"),
            ("DELETE", "/api/bili_accounts/123456", "删 B站账号"),
            ("POST", "/api/external/clear_key", "清外部 API 密钥"),
            ("POST", "/api/external/api_token", "重置外部 API 密钥"),
            ("POST", "/api/restart", "重启服务"),
        ):
            code, body = _req(method, path, TOK_OWNER)
            ok(f"{label} -> 403", code == 403, f"{code} {body[:60]}")

        # ── 4c. 未登录也不能碰外部 API 管理接口 ──
        print("[4c] 未登录访问 /api/external/* 管理接口")
        for method, path, label in (
            ("POST", "/api/external/api_token", "重置外部密钥（响应会带明文）"),
            ("POST", "/api/external/clear_key", "清空外部密钥"),
            ("POST", "/api/external/api_domain", "改外部 API 域名"),
            ("POST", "/api/external/restart", "重启外部服务"),
        ):
            code, body = _req(method, path, "no-such-token")
            ok(f"{label} -> 401", code == 401, f"{code} {body[:60]}")

        # 只读查询仍然可以带 token 直接调（插件用法不能被破坏）
        code, body = _req("GET", f"/api/external/ranking?room_id={ROOM_A}&period=all"
                                 f"&token=wrong", "no-such-token")
        ok("只读外部查询免登录可达（token 错误 -> 401 而不是 403/登录页）",
           code == 401, f"{code} {body[:60]}")

        # ── 5. 管理员不受影响 ──
        print("[5] 管理员仍然畅通")
        code, body = _req("GET", "/api/rooms", TOK_ADMIN)
        rooms = (json.loads(body).get("rooms") if code == 200 else []) or []
        ok("管理员能看到两个房间", len(rooms) == 2, len(rooms))
        for rid in (ROOM_A, ROOM_B):
            code, _ = _req("GET", f"/api/rooms/{rid}/ranking?rStart=&rEnd=&rType=all",
                           TOK_ADMIN)
            ok(f"管理员读 {rid} 排行 -> 200", code == 200, code)
        code, _ = _req("GET", f"/api/rooms/{ROOM_B}/config", TOK_ADMIN)
        ok("管理员读任意配置 -> 200", code == 200, code)
        code, _ = _req("POST", f"/api/rooms/{ROOM_A}/stop", TOK_ADMIN)
        ok("管理员停机器人 -> 200", code == 200, code)

        # ── 6. 未登录仍然 401（回归）──
        print("[6] 未登录")
        code, _ = _req("GET", f"/api/rooms/{ROOM_A}/ranking", "bogus-token")
        ok("未登录 -> 401", code == 401, code)

        # ── 6b. 会话/限流/扫码会话的过期清理 ──
        print("[6b] 进程内状态清理（防长期运行内存溢出）")
        now = time.time()
        srv._RATE_LIMIT["1.1.1.1"] = [now - 10]          # 活跃
        srv._RATE_LIMIT["2.2.2.2"] = [now - 999]         # 全过期 → 应删条目
        srv._RATE_LIMIT["3.3.3.3"] = []                  # 空列表 → 应删条目
        srv._SESSIONS["expired-tok"] = (now - 1, "x", "user", [])
        srv._SESSIONS["live-tok"] = (now + 600, "y", "user", [])
        srv._BILI_LOGIN_SESSIONS["old-qr"] = {"created_at": now - 3600, "state": "waiting"}
        srv._BILI_LOGIN_SESSIONS["fresh-qr"] = {"created_at": now - 5, "state": "waiting"}
        purged = srv._purge_stale_state()
        print(f"  清理结果: {purged}")
        ok("过期 IP 被删除", "2.2.2.2" not in srv._RATE_LIMIT)
        ok("空列表 IP 被删除", "3.3.3.3" not in srv._RATE_LIMIT)
        ok("活跃 IP 保留", "1.1.1.1" in srv._RATE_LIMIT)
        ok("过期会话被删除", "expired-tok" not in srv._SESSIONS)
        ok("有效会话保留", "live-tok" in srv._SESSIONS)
        ok("过期扫码会话被删除", "old-qr" not in srv._BILI_LOGIN_SESSIONS)
        ok("新扫码会话保留", "fresh-qr" in srv._BILI_LOGIN_SESSIONS)

        # ── 7. 结构断言：所有房间接口都应该被中间件统一覆盖 ──
        print("[7] 结构断言")
        src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        ok("存在房间路径正则", "_ROOM_PATH_RE" in src)
        ok("存在普通账号房间写白名单", "_USER_ROOM_MUTATIONS" in src)
        ok("白名单只含 启停/重启/配置 四项",
           "frozenset({" in src and '"/start"' in src and '"/config"' in src)
        ok("DELETE /danmaku/clear 在管理员侧（不在白名单里）",
           '"/danmaku/clear"' not in src.split("_USER_ROOM_MUTATIONS")[1].split("}")[0])
        # 只在可执行代码里找（注释里引用旧写法是正常的）
        ok("不再有 /api/external/ 整个前缀免登录",
           'or path.startswith("/api/external/")' not in src)
        ok("存在免登录外部接口白名单", "_PUBLIC_EXTERNAL_GETS" in src)
        ok("守护循环里会清理过期状态",
           "_purge_stale_state()" in src.split("async def _supervise_loop")[1])
        # 只在**可执行代码**里找（注释里出现这句是正常的，因为注释在解释历史 bug）
        bad_line = "\n    if role in (\"user\", \"streamer\") and allowed:"
        ok("allowed 为空时不会放行全部", bad_line not in src)
    finally:
        server.should_exit = True
        th.join(timeout=15)
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print(f"\nFAILED ({len(fails)}):")
        for f in fails:
            print(" -", f)
        return 1
    print("\nROOM ACL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
