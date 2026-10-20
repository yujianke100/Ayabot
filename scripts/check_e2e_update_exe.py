"""端到端验证：用真实的旧版 exe 检查更新，确认它认得出新版本且推荐 Gitee 线路。

隔离环境：LOCALAPPDATA 指向 scratch 下的临时目录，绝不碰用户真实的 %LOCALAPPDATA%\\Ayabot。

注意：exe 有单实例互斥锁（mutex），本机已经在跑 Ayabot 时会直接退出。这种情况脚本
打印 SKIP 并以 0 退出 —— 想真跑就先关掉正在运行的那个 Ayabot。

用法:
    ./.venv/Scripts/python.exe scripts/check_e2e_update_exe.py <旧版exe> [期望的最新版本]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

EXE = sys.argv[1] if len(sys.argv) > 1 else ""
EXPECT_NEW = (sys.argv[2] if len(sys.argv) > 2 else "").lstrip("vV")
if not EXE or not Path(EXE).exists():
    raise SystemExit("用法: check_e2e_update_exe.py <旧版exe> [期望最新版本]")

PORT = 19871
ROOT = Path(r"C:/Users/shinshi/AppData/Local/hermes/cache/scratch/e2e_update")
ROOT.mkdir(parents=True, exist_ok=True)
LOG = ROOT / "run.log"

env = dict(os.environ)
env["LOCALAPPDATA"] = str(ROOT)


def http(method: str, path: str, body: dict | None = None, cookie: str = "") -> tuple[int, dict, str]:
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode() or "{}"), r.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}"), e.headers.get("Set-Cookie", "")
        except Exception:
            return e.code, {"raw": raw[:300]}, e.headers.get("Set-Cookie", "")


print(f"启动 {Path(EXE).name}（隔离数据目录 {ROOT}，端口 {PORT}）")
with open(LOG, "wb") as log_fh:
    proc = subprocess.Popen([EXE, "--port", str(PORT)], env=env,
                            stdout=log_fh, stderr=subprocess.STDOUT)
try:
    ready = False
    for _ in range(160):
        if proc.poll() is not None:
            text = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
            if "another instance already running" in text:
                print("SKIP：本机已有 Ayabot 在运行（单实例互斥），关掉它再跑这个检查")
                sys.exit(0)
            print("进程提前退出:\n" + text[-1200:])
            raise SystemExit(1)
        for probe in ("/login", "/"):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{probe}", timeout=3):
                    ready = True
                    break
            except urllib.error.HTTPError:
                ready = True          # 401/403 也代表服务起来了
                break
            except Exception:
                continue
        if ready:
            break
        time.sleep(0.5)
    print("服务就绪:", ready)
    if not ready:
        raise SystemExit("服务未就绪")

    token = ""
    for user, pw in (("ayabot", "123456"), ("admin", "admin")):
        code, body, _ = http("POST", "/api/login", {"username": user, "password": pw})
        print(f"登录 {user}:", code, str(body)[:100])
        if code == 200 and isinstance(body, dict):
            token = str(body.get("token") or "")
            break
    cookie = f"session={token}" if token else ""

    started = time.time()
    code, data, _ = http("GET", "/api/update/check?force=1", cookie=cookie)
    elapsed = time.time() - started
    print(f"\n更新检查: HTTP {code} in {elapsed:.2f}s")
    if code != 200:
        print(json.dumps(data, ensure_ascii=False)[:400])
        raise SystemExit(1)

    print("  当前版本 :", data.get("current"))
    print("  最新版本 :", data.get("latest"))
    print("  有更新   :", data.get("has_update"))
    print("  元数据源 :", data.get("meta_source"), f"({data.get('meta_ms')}ms)")
    rec = data.get("recommended") or {}
    print("  推荐线路 :", rec.get("name"), f"{rec.get('ms')}ms")
    print("  推荐地址 :", rec.get("url"))
    print("  文件大小 :", rec.get("total_bytes"), "期望", data.get("expected_bytes"))
    print("\n  全部线路:")
    for d in data.get("downloads") or []:
        print(f"    {d.get('name',''):<22} ok={d.get('ok')!s:<5} ms={d.get('ms')} "
              f"stale={d.get('stale')} bytes={d.get('total_bytes')}")

    checks = {
        "认出有更新": data.get("has_update") is True,
        "版本号正确": (not EXPECT_NEW) or data.get("latest", "").lstrip("vV") == EXPECT_NEW,
        "推荐 Gitee": str(rec.get("name") or "").startswith("gitee"),
        "大小一致": bool(rec.get("total_bytes")) and rec.get("total_bytes") == data.get("expected_bytes"),
    }
    print()
    for k, v in checks.items():
        print(f"  {'OK ' if v else 'MISS'} {k}")
    ok = all(checks.values())
    print("\n结论:", "PASS 真实 exe 能从 Gitee 拿到新版本" if ok else "FAIL")
    sys.exit(0 if ok else 2)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    print("已退出 exe")
