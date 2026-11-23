"""第二十三轮（后半）自检：配置保存失败时返回 200 + {"ok": false}。

背景：第十三轮定下的原则是「**永远不要 200 + error**」——那会让
'坏了' 和 '没数据' 分不清，是「统计全是 0」的根因。但那条原则当时只用在
**读**接口上，写接口原样留了下来：

    POST /api/rooms/{id}/config      -> {"ok": ok}
    POST /api/rooms/{id}/config/import -> {"ok": ok}
    POST /api/general_config         -> {"ok": ok}

前端有多处只判断 `res.ok`：
 - 保存 AI 配置时「顺带写房间配置」：`if (res2.ok)` → 失败也当成功，
   内存里的 roomConfig 被改成新值，磁盘上还是旧的 → 下次重启房间
   用的仍是旧 LLM 设置，用户以为「AI 设置不生效」。
 - 新建房间后「关联 B站账号」「套用预设模板」：两个 fetch 的响应直接丢弃，
   一律提示「✅ 创建成功」，模板其实没写进去。

修复：三个写接口保存失败一律返回 500；前端把「房间那份写失败」
和「模板/账号没配上」显式说出来（房间是建成了，但不能说全成功）。

用法: ./.venv/Scripts/python.exe scripts/check_config_save_contract.py
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

PORT = 19890
ROOM = "1900001000"
TOK = "tok-admin-23"
SRC = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")


def _req(method: str, path: str, token: str = TOK,
         body: bytes | None = None) -> tuple[int, str]:
    headers = {"Cookie": f"session={token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    import uvicorn
    import yaml

    import app.config as appcfg
    from app.web import server as srv

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_save_"))
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    room_dir = tmp / "rooms" / ROOM
    (room_dir / "data").mkdir(parents=True, exist_ok=True)
    (room_dir / "config.yaml").write_text(
        yaml.safe_dump(example, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (tmp / "config.yaml").write_text(
        yaml.safe_dump(example, allow_unicode=True, sort_keys=False), encoding="utf-8")

    srv._ROOMS_BASE_DIR = str(tmp)
    srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
    srv.set_rooms_base_dir(str(tmp))
    srv._ROOM_DB_PATH_CACHE.clear()
    srv._rate_limited = lambda ip: False  # type: ignore[assignment]
    srv._login_limited = lambda ip, username="": False  # type: ignore[assignment]
    srv._SESSIONS[TOK] = (time.time() + 900, "root", "admin", [])

    real_update = appcfg.update_config_from_dict

    server = uvicorn.Server(uvicorn.Config(srv.app, host="127.0.0.1", port=PORT,
                                          log_level="critical"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(80):
        if server.started:
            break
        time.sleep(0.25)
    if not server.started:
        print("FAILED: uvicorn 未启动")
        return 1

    try:
        # ── 1. 三个写接口：失败必须 5xx ──
        print("[1] 保存失败时不能返回 200")
        appcfg.update_config_from_dict = lambda *a, **k: False  # type: ignore[assignment]
        for path, label in (
            (f"/api/rooms/{ROOM}/config", "房间配置保存"),
            (f"/api/rooms/{ROOM}/config/import", "房间配置导入"),
            ("/api/general_config", "全局配置保存"),
        ):
            code, body = _req("POST", path, body=json.dumps({"bot_name": "x"}).encode())
            ok(f"{label} 失败 -> 500", code == 500, f"{code} {body[:70]}")
            ok(f"{label} 失败时没有 ok:true", '"ok": true' not in body.replace(" ", " "),
               body[:60])
        appcfg.update_config_from_dict = real_update  # type: ignore[assignment]

        # ── 2. 成功路径仍然是 200 ──
        print("[2] 保存成功仍然是 200 且 ok:true")
        code, body = _req("POST", f"/api/rooms/{ROOM}/config",
                          body=json.dumps({"bot_name": "新名字"}).encode())
        ok("房间配置保存 -> 200", code == 200, f"{code} {body[:60]}")
        ok("返回 ok:true", json.loads(body).get("ok") is True, body[:60])
        saved = yaml.safe_load((room_dir / "config.yaml").read_text(encoding="utf-8"))
        ok("磁盘上确实写进去了", saved.get("web_ui", {}).get("bot_name") == "新名字",
           saved.get("web_ui", {}).get("bot_name"))

        code, body = _req("POST", "/api/general_config",
                          body=json.dumps({"bot_name": "全局"}).encode())
        ok("全局配置保存 -> 200", code == 200, f"{code}")
        gen = yaml.safe_load((tmp / "config.yaml").read_text(encoding="utf-8"))
        ok("全局磁盘写入生效", gen.get("web_ui", {}).get("bot_name") == "全局",
           gen.get("web_ui", {}).get("bot_name"))

        # ── 3. 结构断言：前端不再吞掉失败 ──
        print("[3] 前端把「部分失败」说出来")
        ok("房间配置响应不再只看 res.ok 就更新内存",
           "roomMirrorWarn" in SRC)
        ok("AI 保存会带上房间写入告警",
           "全局已保存，但' + roomMirrorWarn" in SRC)
        ok("新建房间收集软失败", "const softWarns = []" in SRC)
        ok("模板套用失败会提示", "预设模板套用失败" in SRC)
        ok("账号关联失败会提示", "B站账号关联失败" in SRC)
        ok("有软失败时不报纯成功", "房间 ${data.room_id} 已创建，但：" in SRC)
        ok("后端不再有 `return {\"ok\": ok}` 写法",
           '{"ok": ok}' not in SRC, SRC.count('{"ok": ok}'))
        ok("三个写接口都改成 5xx",
           SRC.count('"error": "配置保存失败（详见服务端日志）"') == 1
           and SRC.count('"error": "配置导入失败（详见服务端日志）"') == 1
           and SRC.count('"error": "全局配置保存失败（详见服务端日志）"') == 1,
           [SRC.count('"error": "配置保存失败（详见服务端日志）"'),
            SRC.count('"error": "配置导入失败（详见服务端日志）"'),
            SRC.count('"error": "全局配置保存失败（详见服务端日志）"')])

        # ── 4. update_config_from_dict 仍是「合并」而不是「整份替换」 ──
        print("[4] 保存不能把没提交的字段清掉")
        before = yaml.safe_load((room_dir / "config.yaml").read_text(encoding="utf-8"))
        before_kw = json.dumps(before.get("features", {}).get("keyword_reply"), sort_keys=True)
        _req("POST", f"/api/rooms/{ROOM}/config",
             body=json.dumps({"bot_name": "再改一次"}).encode())
        after = yaml.safe_load((room_dir / "config.yaml").read_text(encoding="utf-8"))
        after_kw = json.dumps(after.get("features", {}).get("keyword_reply"), sort_keys=True)
        ok("只传 bot_name 时 keyword_reply 不变", before_kw == after_kw)
        ok("room_display_id 也保留", after.get("room_display_id") == before.get("room_display_id"))
        ok("storage 段保留", after.get("storage") is not None)
    finally:
        appcfg.update_config_from_dict = real_update  # type: ignore[assignment]
        server.should_exit = True
        time.sleep(0.4)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("CONFIG SAVE CONTRACT OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
