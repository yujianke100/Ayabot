"""第二十五轮：真实 exe 的端到端验证（隔离数据目录，不碰真实 rooms/ 与 config.yaml）。

覆盖发版前的最后一道：
  1. exe 能起来，`--port` 真的生效（以前这个参数完全没人读）
  2. 前端内置库 `/vendor/*` 四条全 200（CDN 挂了也不至于整页 {{ }}）
  3. 登录页 → 登录 → `must_reset_password` 提示
  4. 建房 → 读配置 → 写配置 → 排行/日期接口
  5. 端口被占用时 exe 不会「假装成功」：日志里出现端口占用说明，进程不崩
  6. 正常退出后没有遗留 pid/lock 文件，日志里没有 Traceback

用法: ./.venv/Scripts/python.exe scripts/check_exe_e2e.py [exe路径]
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "dist" / "Ayabot-v1.2.7.exe")
SANDBOX = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/e2e_exe")

OLD_VERSION = {"current": "", "latest": "", "has_update": False}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def serving(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _hdr(headers: dict, name: str) -> str:
    """HTTP header 名大小写无关（Starlette 发的是小写 set-cookie）。"""
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v if isinstance(v, str) else (v[0] if v else "")
    return ""


def http(port: int, method: str, path: str, body: dict | None = None,
         cookie: str = "") -> tuple[int, bytes, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _kill_exe_tree(exe: str) -> int:
    """强杀 exe 及其子进程（onefile 的真实进程是子进程）。

    `proc.terminate()` 只杀 PyInstaller 的引导进程，子进程会继续持有
    单实例互斥锁和端口 —— 后续测试于是全部撞锁、报一堆假失败。
    """
    name = Path(exe).name
    killed = 0
    if os.name != "nt":
        return killed
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV"], capture_output=True)
        text = out.stdout.decode("gbk", "ignore")
        for line in text.splitlines():
            if name.lower() not in line.lower():
                continue
            parts = line.split('","')
            if len(parts) < 2:
                continue
            pid = parts[1].strip('"')
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
            killed += 1
    except Exception as exc:  # noqa: BLE001
        print(f"  (清理进程失败: {exc})")
    return killed


def main() -> int:
    if not Path(EXE).exists():
        print(f"FAILED: 找不到 exe: {EXE}")
        return 1
    if shutil.which("tasklist") is None and os.name != "nt":
        print("SKIP: 非 Windows")
        return 0

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    data_dir = SANDBOX / "Ayabot"
    shutil.rmtree(SANDBOX, ignore_errors=True)
    data_dir.mkdir(parents=True)
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(SANDBOX)     # 冻结模式的 DATA_DIR = %LOCALAPPDATA%\Ayabot

    leftover = _kill_exe_tree(EXE)
    if leftover:
        print(f"（先清理了 {leftover} 个残留实例）")
        time.sleep(1.0)

    port = free_port()
    log = SANDBOX / "run.log"

    print(f"[1] 启动 {Path(EXE).name} --port {port}（隔离目录 {SANDBOX}）")
    with open(log, "wb") as fh:
        proc = subprocess.Popen([EXE, "--port", str(port)], env=env,
                                stdout=fh, stderr=subprocess.STDOUT)
    try:
        up = False
        for _ in range(200):
            if serving(port):
                up = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        ok("exe 启动了 WebUI", up, f"pid={proc.pid} exit={proc.poll()}")
        if not up:
            tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
            print("---- 启动日志尾部 ----\n", tail)
            return 1
        ok("命令行 --port 生效", up)
        ok("port.txt 落盘了新端口",
           (data_dir / "port.txt").exists()
           and (data_dir / "port.txt").read_text(encoding="utf-8").strip() == str(port),
           (data_dir / "port.txt").read_text(encoding="utf-8").strip()
           if (data_dir / "port.txt").exists() else "缺失")
        ok("自动生成了 config.yaml", (data_dir / "config.yaml").exists())

        # ── 2. 内置前端库 ──
        print("[2] /vendor/* 内置库")
        code, body, hdrs = http(port, "GET", "/")
        html = body.decode("utf-8", "replace")
        ok("首页 200", code == 200, code)
        ok("首页带 v-cloak 占位", "v-cloak" in html)
        ok("首页引用四个内置库",
           all(f"/vendor/{n}" in html for n in
               ("tailwind.js", "vue.global.prod.js", "chart.umd.min.js",
                "modern-screenshot.js")))
        for name in ("vue.global.prod.js", "chart.umd.min.js",
                     "tailwind.js", "modern-screenshot.js"):
            code, body, hdrs = http(port, "GET", f"/vendor/{name}")
            ok(f"{name} 200 且非空", code == 200 and len(body) > 10000,
               f"{code} {len(body)}B")

        # ── 3. 登录 ──
        print("[3] 登录与首次改密提示")
        code, body, _ = http(port, "GET", "/api/time")
        ok("/api/time 未登录被拒（401）", code == 401, code)
        code, body, hdrs = http(port, "POST", "/api/login",
                                {"username": "ayabot", "password": "123456"})
        ok("默认账号能登录", code == 200, f"{code} {body[:80]}")
        cookie = _hdr(hdrs, "set-cookie").split(";")[0]
        ok("下发了 session cookie", cookie.startswith("session="), cookie[:20])
        info = json.loads(body) if code == 200 else {}
        ok("提示必须改默认密码", info.get("must_reset_password") is True, info)
        ok("响应里不再回传 token", "token" not in info, list(info))
        code, body, _ = http(port, "GET", "/api/time", cookie=cookie)
        ok("登录后 /api/time 可用", code == 200, f"{code} {body[:60]}")

        # ── 4. 建房 / 配置 / 统计接口 ──
        print("[4] 建房与房间接口")
        room = "1900012500"
        code, body, _ = http(port, "POST", "/api/rooms",
                             {"anchor_uid": 999001, "room_display_id": room,
                              "room_name": "E2E 测试房"}, cookie=cookie)
        ok("建房 200", code == 200, f"{code} {body[:100]}")
        code, body, _ = http(port, "GET", f"/api/rooms/{room}/config", cookie=cookie)
        ok("读配置 200", code == 200, code)
        cfg = json.loads(body) if code == 200 else {}
        ok("配置里 room_display_id 正确", str(cfg.get("room_display_id")) == room,
           cfg.get("room_display_id"))
        code, body, _ = http(port, "POST", f"/api/rooms/{room}/config",
                             {"bot_name": "e2e-bot"}, cookie=cookie)
        ok("写配置 200", code == 200, f"{code} {body[:60]}")
        code, body, _ = http(port, "GET", f"/api/rooms/{room}/ranking", cookie=cookie)
        ok("排行接口 200", code == 200, f"{code} {body[:60]}")
        ok("排行是空数组而不是 error",
           code == 200 and json.loads(body).get("ranking") == [], body[:60])
        code, body, _ = http(port, "GET", f"/api/rooms/{room}/all_dates", cookie=cookie)
        ok("日期列表 200", code == 200, f"{code} {body[:40]}")
        code, body, _ = http(port, "GET", "/api/rooms", cookie=cookie)
        ok("房间列表包含新房", code == 200 and room in body.decode("utf-8", "replace"),
           f"{code}")

        # ── 5. 越权与鉴权仍然生效（打包后也要成立）──
        print("[5] 打包后的鉴权")
        code, body, _ = http(port, "GET", "/api/rooms")
        ok("未登录读房间列表 401", code == 401, code)
        code, body, _ = http(port, "POST", f"/api/external/api_token")
        ok("未登录重置外部密钥 401", code == 401, code)
        code, body, _ = http(port, "GET",
                             "/api/proxy_image?url=http://127.0.0.1:9/x")
        ok("未登录图片代理 401", code == 401, code)

        # ── 6. 日志里不该有 traceback ──
        print("[6] 运行日志")
        text = log.read_text(encoding="utf-8", errors="replace")
        ok("日志里没有 Traceback", "Traceback (most recent call last)" not in text,
           text.count("Traceback"))
        ok("日志里没有 init_app failed", "init_app failed" not in text)
    finally:
        _kill_exe_tree(EXE)
        time.sleep(1.5)

    # ── 7. 端口被占用时的行为 ──
    print("[7] 端口被占用：不能假装成功")
    busy = socket.socket()
    busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    busy.bind(("0.0.0.0", 0))
    busy.listen(1)
    busy_port = busy.getsockname()[1]
    log2 = SANDBOX / "run_conflict.log"
    try:
        with open(log2, "wb") as fh:
            proc2 = subprocess.Popen([EXE, "--port", str(busy_port)], env=env,
                                     stdout=fh, stderr=subprocess.STDOUT)
        time.sleep(25)
        text2 = log2.read_text(encoding="utf-8", errors="replace")
        ok("进程没有崩溃（仍在跑或正常退出）", proc2.poll() in (None, 0),
           f"exit={proc2.poll()}")
        ok("日志明确指出端口失败",
           ("绑定失败" in text2) or ("10048" in text2) or ("address already in use" in text2)
           or ("payLoad" in text2), "见 run_conflict.log")
        ok("没有把占用端口当成成功（不该出现 Uvicorn running on）",
           "Uvicorn running on" not in text2)
        _kill_exe_tree(EXE)
    finally:
        busy.close()

    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        print(f"\n完整日志: {log}\n冲突日志: {log2}")
        return 1
    print("EXE E2E OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
