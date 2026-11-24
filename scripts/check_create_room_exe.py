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
if not EXE:
    # 不给参数时自动用最新的本地构建产物（否则整批门禁都跑不成）
    root = Path(__file__).resolve().parent.parent
    built = sorted((root / "dist").glob("Ayabot-v*.exe"))
    EXE = str(built[-1]) if built else ""
if not EXE or not Path(EXE).exists():
    print("SKIP：没有可用的 exe（先构建，或显式传入路径）")
    raise SystemExit(0)

ROOT = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/room_exe")
LOG = ROOT / "run.log"


def _kill_exe_tree(exe: str) -> int:
    """强杀 exe 及其子进程（PyInstaller onefile 的真实进程是子进程）。

    只 terminate 引导进程的话，子进程会继续持有单实例互斥锁和端口，
    下一次运行直接撞锁 → 报出一堆看不懂的假失败。
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


leftover = _kill_exe_tree(EXE)
if leftover:
    print(f"清掉了 {leftover} 个残留进程")
    time.sleep(1)

shutil.rmtree(ROOT, ignore_errors=True)
ROOT.mkdir(parents=True, exist_ok=True)

env = dict(os.environ)
env["LOCALAPPDATA"] = str(ROOT)
DATA = ROOT / "Ayabot"


def http(method: str, path: str, body: dict | None = None,
         cookie: str = "") -> tuple[int, dict, str]:
    """返回 (status, body, 新 session cookie)。

    第 21 轮起 `/api/login` **不再在响应体里回传 token**（cookie 是 httponly），
    所以 session 必须从 `Set-Cookie` 取，不能读 `body["token"]`。
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw, headers, status = r.read(), r.headers, r.status
    except urllib.error.HTTPError as e:
        raw, headers, status = e.read(), e.headers, e.code
    try:
        parsed = json.loads(raw.decode() or "{}")
    except Exception:
        parsed = {"raw": raw.decode("utf-8", "replace")[:300]}
    new_cookie = ""
    for val in headers.get_all("Set-Cookie") or []:
        first = val.split(";", 1)[0].strip()
        if first.startswith("session="):
            new_cookie = first
    return status, parsed, new_cookie


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

    cookie = ""
    for user, pw in (("ayabot", "123456"), ("admin", "admin")):
        code, body, new = http("POST", "/api/login", {"username": user, "password": pw})
        print(f"登录 {user}: HTTP {code} {str(body)[:90]} cookie={new[:16] or '(空)'}")
        if code == 200 and new:
            cookie = new
            break
    if not cookie:
        print("结论: FAIL 登录没拿到 session cookie")
        sys.exit(2)

    code, body, _ = http("POST", "/api/rooms", {
        "anchor_uid": 672328094, "room_display_id": 21452505, "room_name": "测试房间",
    }, cookie=cookie)
    print(f"\n新建直播间: HTTP {code} {json.dumps(body, ensure_ascii=False)[:200]}")
    ids: list[str] = []
    if code == 200:
        _, body2, _ = http("GET", "/api/rooms", cookie=cookie)
        ids = [str(r.get("room_id")) for r in (body2.get("rooms") or [])]
        print(f"房间列表: {ids}")
    # 房间目录必须真的建出来，并且能被列表接口看到
    rooms_base = DATA / "rooms"
    on_disk = sorted(p.name for p in rooms_base.iterdir()) if rooms_base.exists() else []
    print(f"磁盘上的房间目录: {on_disk}")
    ok = code == 200 and "21452505" in ids and "21452505" in on_disk
    print("\n结论:", "PASS 能新建" if ok else "FAIL 复现了")
    sys.exit(0 if ok else 2)
finally:
    # onefile：必须杀进程树，否则残留会锁住下一次运行
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    _kill_exe_tree(EXE)
    print("（日志尾部）")
    print("".join((LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else "")[-1200:]))
