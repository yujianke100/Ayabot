"""用真实 exe 复现「新建直播间 → 根目录 config.yaml 不存在」。

隔离环境（LOCALAPPDATA 指到 scratch），不会碰用户真实数据。
用法: ./.venv/Scripts/python.exe scripts/check_create_room_exe.py <exe> [端口]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

EXE = sys.argv[1] if len(sys.argv) > 1 else ""
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 19874
if not EXE or not Path(EXE).exists():
    raise SystemExit("用法: check_create_room_exe.py <exe> [端口]")

ROOT = Path(r"C:/Users/shinshi/AppData/Local/hermes/cache/scratch/room_exe")
shutil.rmtree(ROOT, ignore_errors=True)
ROOT.mkdir(parents=True, exist_ok=True)
LOG = ROOT / "run.log"

env = dict(os.environ)
env["LOCALAPPDATA"] = str(ROOT)
DATA = ROOT / "Ayabot"


def http(method: str, path: str, body: dict | None = None, cookie: str = "") -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except Exception:
            return e.code, {"raw": raw[:300]}


print(f"启动 {Path(EXE).name}（LOCALAPPDATA={ROOT}，端口 {PORT}）")
with open(LOG, "wb") as fh:
    proc = subprocess.Popen([EXE, "--port", str(PORT)], env=env, stdout=fh, stderr=subprocess.STDOUT)
try:
    ready = False
    for _ in range(160):
        if proc.poll() is not None:
            text = LOG.read_text(encoding="utf-8", errors="replace")
            if "another instance already running" in text:
                print("SKIP：本机已有 Ayabot 在运行（单实例互斥）")
                sys.exit(0)
            print("进程提前退出:\n" + text[-1500:])
            raise SystemExit(1)
        for probe in ("/login", "/"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}{probe}", timeout=3)
                ready = True
            except urllib.error.HTTPError:
                ready = True
            except Exception:
                pass
            if ready:
                break
        if ready:
            break
        time.sleep(0.5)
    print("服务就绪:", ready)
    if not ready:
        raise SystemExit("服务未就绪")

    print("数据目录内容:", sorted(p.name for p in DATA.iterdir()) if DATA.exists() else "（不存在）")

    token = ""
    for user, pw in (("ayabot", "123456"), ("admin", "admin")):
        code, body = http("POST", "/api/login", {"username": user, "password": pw})
        print(f"登录 {user}: HTTP {code} {str(body)[:90]}")
        if code == 200:
            token = str(body.get("token") or "")
            break
    cookie = f"session={token}" if token else ""

    code, body = http("POST", "/api/rooms", {
        "anchor_uid": 672328094, "room_display_id": 21452505, "room_name": "测试房间",
    }, cookie=cookie)
    print(f"\n新建直播间: HTTP {code} {json.dumps(body, ensure_ascii=False)[:200]}")
    if code == 200:
        code2, body2 = http("GET", "/api/rooms", cookie=cookie)
        ids = [r.get("room_id") for r in (body2.get("rooms") or [])]
        print(f"房间列表: {ids}")
    print("\n结论:", "PASS 能新建" if code == 200 else "FAIL 复现了")
    sys.exit(0 if code == 200 else 2)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("（日志尾部）")
    print("".join((LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else "")[-1200:]))
