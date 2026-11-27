"""配置文件写错时，用户到底看到什么。

用户会手改 `config.yaml`：少一个字段、把数字写成「两秒」、用 Tab 缩进、
记事本存成 GBK、端口写成 70000……这些都是真实发生过的。

以前的样子：
- `KeyError: 'anchor_uid'` / `ValueError: invalid literal for int() with base 10`
  / `yaml.ScannerError: while scanning for the next token ...`
- 界面上只有 alert「启动失败: {"error":"启动失败，请查看该房间的日志"}」
- 用户去翻 200 行配置，完全不知道是哪一项、错在哪

本脚本断言：
  [1] 每个典型错误都变成 `ConfigError`，消息里带**文件名 + 字段名 + 当前值**
  [2] 必填项缺失也点名
  [3] 端口范围有校验（0 和 70000 都会变成「服务起不来」且无提示）
  [4] YAML 语法错误带行号，Tab 缩进有专门提示
  [5] 非 UTF-8 文件提示换编码（Windows 记事本默认是 GBK）
  [6] `load_config` 不会漏出 KeyError/ValueError/ScannerError
  [7] 房间启动失败时，接口把原因带给前端（不是干巴巴一句「请查看日志」）

    ./.venv/Scripts/python.exe scripts/check_config_errors.py
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

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 18983
ROOM = "1946287911"
TOKEN = "cfg-err"


def main() -> int:
    from app.config import ConfigError, load_config

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    base = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp(prefix="cfgerr_"))

    def write_cfg(raw) -> Path:
        p = tmp / f"c{time.time_ns()}.yaml"
        p.write_text(yaml.dump(raw, allow_unicode=True), encoding="utf-8")
        return p

    def mutate(dotted: str, value):
        raw = yaml.safe_load(yaml.dump(base))
        parts = dotted.split(".")
        cur = raw
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
        return raw

    def expect_error(label: str, raw, must_contain: tuple[str, ...]) -> None:
        p = write_cfg(raw) if isinstance(raw, dict) else raw
        try:
            load_config(str(p))
        except ConfigError as exc:
            msg = str(exc)
            missing = [m for m in must_contain if m not in msg]
            ok(f"{label:34s}", not missing,
               f"缺少 {missing}；实际: {msg[-110:]}" if missing else msg[len(str(p)) + 8:][:80])
            return
        except Exception as exc:  # noqa: BLE001
            ok(f"{label:34s}", False, f"漏出 {type(exc).__name__}: {exc}")
            return
        ok(f"{label:34s}", False, "居然加载成功了")

    print("[0] 段落形状写错（缩进掉了 / 写成一行）也要点名到字段")
    # 这类错误以前抛的是 `AttributeError: 'str' object has no attribute 'get'`：
    # 既不是 ConfigError（main.py 不接），也完全不说哪个字段。
    for section in ("features", "cooldown", "rate_limit", "runtime", "storage", "auth",
                    "llm", "web_ui"):
        expect_error(f"{section} 写成字符串", mutate(section, "随便写点什么"),
                     (section, "键值对"))
        expect_error(f"{section} 写成列表", mutate(section, [1, 2]), (section, "键值对"))

    print("[1] 常见手改错误都要点名到字段")
    expect_error("anchor_uid 写成字母", mutate("anchor_uid", "abc"),
                 ("anchor_uid", "主播 UID", "abc"))
    expect_error("room_display_id 写成汉字", mutate("room_display_id", "一号厅"),
                 ("room_display_id", "直播间号"))
    expect_error("发送间隔写成「两秒」", mutate("rate_limit.send_interval_seconds", "两秒"),
                 ("send_interval_seconds", "两秒"))
    expect_error("队列长度写成「50个」", mutate("rate_limit.max_queue_size", "50个"),
                 ("max_queue_size", "50个"))
    expect_error("弹幕保留天数写成浮点串", mutate("features.danmaku_retention_days", "15天"),
                 ("danmaku_retention_days",))

    print("[2] 必填项缺失要点名")
    raw = yaml.safe_load(yaml.dump(base)); raw.pop("anchor_uid")
    expect_error("缺 anchor_uid", raw, ("缺少必填项", "anchor_uid", "主播 UID"))
    raw = yaml.safe_load(yaml.dump(base)); raw.pop("room_display_id")
    expect_error("缺 room_display_id", raw, ("缺少必填项", "room_display_id"))

    print("[3] 端口范围")
    expect_error("端口 70000", mutate("web_ui.port", 70000), ("web_ui.port", "1–65535", "70000"))
    expect_error("端口 0", mutate("web_ui.port", 0), ("web_ui.port", "1–65535"))
    expect_error("端口写成字符串", mutate("web_ui.port", "八千"), ("web_ui.port", "八千"))

    print("[4] YAML 语法错误：要带行号，Tab 缩进要给专门提示")
    p = tmp / "tab.yaml"
    p.write_text("anchor_uid:\t1\n", encoding="utf-8")
    expect_error("Tab 缩进", p, ("第 1 行", "Tab", "空格"))
    p = tmp / "indent.yaml"
    p.write_text("anchor_uid: 1\n  room_display_id: 2\n", encoding="utf-8")
    expect_error("缩进多了一层", p, ("第 2 行", "缩进"))
    p = tmp / "root_list.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    expect_error("根节点是列表", p, ("根节点", "list"))

    print("[5] 非 UTF-8（Windows 记事本另存 GBK）")
    p = tmp / "gbk.yaml"
    p.write_bytes("anchor_uid: 1\n".encode("gbk") + b"\xd6\xd0\xce\xc4")
    expect_error("GBK 编码", p, ("UTF-8", "另存为"))

    print("[6] 空配置也要说人话")
    p = tmp / "empty.yaml"
    p.write_text("", encoding="utf-8")
    expect_error("空文件", p, ("缺少必填项",))

    print("[7] 启动失败时接口要把原因带给前端")
    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    (tmp / "config.yaml").write_text(example, encoding="utf-8")
    room = tmp / "rooms" / ROOM
    (room / "data").mkdir(parents=True, exist_ok=True)
    bad = yaml.safe_load(example)
    bad["anchor_uid"] = "abc"
    (room / "config.yaml").write_text(yaml.dump(bad, allow_unicode=True), encoding="utf-8")

    import uvicorn
    from app import process_manager as pm
    from app.web import server as srv

    # 子进程是 `python -m app.main --room ...`，cwd=tmp；源码模式下必须把仓库根
    # 放进 PYTHONPATH，否则它连 `app` 都 import 不到，测不到「配置写错」这条路径。
    import os
    os.environ["PYTHONPATH"] = str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")
    pm.set_rooms_base_dir(str(tmp))
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

    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/rooms/{ROOM}/start",
                                 data=b"", method="POST")
    req.add_header("Cookie", f"session={TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code, body = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        code, body = exc.code, exc.read().decode("utf-8", "replace")
    print(f"  POST /start -> HTTP {code} {body[:160]}")
    ok("接口返回 5xx（起不来就是起不来）", code >= 500, f"HTTP {code}")
    ok("错误信息里有字段名", "anchor_uid" in body, body[:120])
    ok("错误信息里有当前值", "abc" in body, body[:120])
    ok("不是干巴巴一句「请查看日志」", "请查看该房间的日志" not in body, body[:120])
    try:
        json.loads(body)
    except Exception:
        ok("响应是合法 JSON", False, body[:60])

    server.should_exit = True
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails[:6]}")
        return 1
    print("CONFIG ERRORS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
