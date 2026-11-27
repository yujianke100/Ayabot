"""A/B：v1.2.6 与当前 HEAD 的全接口逐字段对比（只读）。

做法：两个独立子进程，各自 cwd 指向自己的代码目录（v1.2.6 用 git worktree），
各自拿一份**同一份真实库的副本**，然后用同一组只读 GET 打两遍，逐字段 diff。

不是读 diff 猜影响 —— 是真把两版跑起来对比输出。这样才回答得了
「相比于 v1.2.6 还有没有回归」。

    ./.venv/Scripts/python.exe scripts/probe_ab_v126.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch")
OLD_SRC = SCRATCH / "ab126"           # git worktree v1.2.6
ROOM = "1946287911"
REAL_DB = ROOT / "rooms" / ROOM / "data" / "bot.db"
PORT_OLD = 18771
PORT_NEW = 18772
USER, PASS = "abtest", "abtest123"

LAUNCHER = r'''
import sys, json, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.config import load_config
from app.web import server as srv
import uvicorn

cfg_path = sys.argv[2]
port = int(sys.argv[3])
cfg = load_config(cfg_path)
srv.init_app(cfg, config_path=cfg_path)
srv._rate_limited = lambda ip: False
srv._login_limited = lambda ip, user="": False
uvicorn.run(srv.app, host="127.0.0.1", port=port, log_level="critical")
'''


def prepare(sandbox: Path, src_root: Path) -> str:
    """搭一份隔离部署：rooms/<id>/config.yaml + 真实库副本。"""
    import yaml
    sandbox.mkdir(parents=True, exist_ok=True)
    example = (src_root / "config.example.yaml").read_text(encoding="utf-8")
    src = yaml.safe_load(example)
    src["room_display_id"] = int(ROOM)
    src["anchor_uid"] = 8888
    src["account_uid"] = ""
    # 和真实部署一致：根 config 相对项目根，房间 config 相对**房间目录**
    # （房间目录里写 "data/bot.db"；写成 "rooms/<id>/data/bot.db" 会被解析成
    #  rooms/<id>/rooms/<id>/data/bot.db —— 我第一版就是这么写错的，
    #  导致当前版全部接口取不到库，看起来像"数据全没了"）
    src["storage"] = {"sqlite_path": f"rooms/{ROOM}/data/bot.db"}
    src["runtime"] = dict(src.get("runtime") or {}, timezone="Asia/Shanghai", log_level="CRITICAL")
    web = src.setdefault("web_ui", {})
    web.update(username=USER, password=PASS, api_token="abtoken", api_enabled=True)
    # 关掉所有会发弹幕的功能，避免任何意外
    f = src.setdefault("features", {})
    for k in ("welcome_enabled", "thanks_enabled", "guard_thanks_enabled",
              "connected_message_enabled", "periodic_message_enabled", "pk_report_enabled"):
        f[k] = False
    (sandbox / "config.yaml").write_text(
        yaml.safe_dump(src, allow_unicode=True, sort_keys=False), encoding="utf-8")
    room = sandbox / "rooms" / ROOM
    (room / "data").mkdir(parents=True, exist_ok=True)
    room_src = dict(src)
    room_src["storage"] = {"sqlite_path": "data/bot.db"}
    (room / "config.yaml").write_text(
        yaml.safe_dump(room_src, allow_unicode=True, sort_keys=False), encoding="utf-8")
    shutil.copy(REAL_DB, room / "data" / "bot.db")
    return str(sandbox / "config.yaml")


def start(src_root: Path, sandbox: Path, cfg_path: str, port: int) -> subprocess.Popen:
    launcher = sandbox / "_launch.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(launcher), str(src_root), cfg_path, str(port)],
        cwd=str(sandbox), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    for _ in range(300):
        if proc.poll() is not None:
            err = proc.stderr.read().decode("utf-8", "replace")[-1500:]
            raise RuntimeError(f"服务起不来（port={port}）：\n{err}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2):
                return proc
        except urllib.error.HTTPError:
            return proc
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    raise RuntimeError(f"服务 30s 未就绪（port={port}）")


class Client:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.cookie = ""

    def _req(self, path: str, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)
        except Exception as exc:  # noqa: BLE001
            return 0, str(exc).encode(), {}

    def login(self) -> None:
        code, body, headers = self._req("/api/login", "POST",
                                       {"username": USER, "password": PASS})
        if code != 200:
            raise RuntimeError(f"登录失败 {code} {body[:200]}")
        for k, v in headers.items():
            if k.lower() == "set-cookie":
                self.cookie = v.split(";")[0]
        if not self.cookie:
            raise RuntimeError("登录没拿到 cookie")

    def get(self, path: str):
        code, body, _ = self._req(path)
        try:
            return code, json.loads(body)
        except Exception:  # noqa: BLE001
            return code, body.decode("utf-8", "replace")[:200]


#: 只读接口清单。新版本额外有的接口会单独标注（不算回归）。
SHARED = [
    "/api/rooms",
    f"/api/rooms/{ROOM}/config",
    f"/api/rooms/{ROOM}/ranking?rStart=&rEnd=&rType=all",
    f"/api/rooms/{ROOM}/ranking?rStart=&rEnd=&rType=blindbox",
    f"/api/rooms/{ROOM}/ranking?rStart=&rEnd=&rType=gift",
    f"/api/rooms/{ROOM}/guards?rStart=&rEnd=",
    f"/api/rooms/{ROOM}/all_dates",
    f"/api/rooms/{ROOM}/danmaku/dates",
    f"/api/danmaku_log?room_id={ROOM}&limit=50&offset=0&date_from=&date_to=&asc=0",
    f"/api/rooms/{ROOM}/export_all?date_from=&date_to=&gift_type=all&sort=ts",
    f"/api/rooms/{ROOM}/export_all?date_from=&date_to=&gift_type=blindbox&sort=value",
    f"/api/rooms/{ROOM}/imports",
    f"/api/rooms/{ROOM}/stats_check",
    "/api/time",
    "/api/general_config",
    "/api/llm_config",
    "/api/templates",
    "/api/users",
    f"/api/external/ranking?room_id={ROOM}&token=abtoken&limit=50",
    f"/api/external/room_stats?room_id={ROOM}&token=abtoken",
    f"/api/ranking?room_id={ROOM}",
    f"/api/user_dates?room_id={ROOM}",
]

#: v1.2.6 就有、但需要真实 uid 的接口（uid 从库里查）
UID_TEMPLATES = [
    f"/api/rooms/{ROOM}/user_dates?uid={{uid}}",
    f"/api/rooms/{ROOM}/user_gifts?uid={{uid}}&rStart=&rEnd=",
    f"/api/external/user_stats?room_id={ROOM}&token=abtoken&uid={{uid}}",
    f"/api/user_gifts?room_id={ROOM}&uid={{uid}}&rStart=&rEnd=",
]


def real_uids() -> list[int]:
    import sqlite3
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT uid, COUNT(*) c FROM gift_events GROUP BY uid ORDER BY c DESC LIMIT 2"
    ).fetchall()
    conn.close()
    return [int(r[0]) for r in rows]


def diff(path: str, old, new, out: list[str]) -> None:
    """逐字段比较；只报「v1.2.6 有、现在没有/变了」。"""
    code_o, val_o = old
    code_n, val_n = new
    if code_o != code_n:
        out.append(f"  [状态码] {path}\n     v1.2.6={code_o}  现在={code_n}")
        return

    def walk(a, b, prefix=""):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in a:
                if k not in b:
                    out.append(f"  [缺字段] {path} :: {prefix}{k}（v1.2.6 有，现在没了）")
                else:
                    walk(a[k], b[k], f"{prefix}{k}.")
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                out.append(f"  [条数] {path} :: {prefix or 'items'} "
                           f"v1.2.6={len(a)} 现在={len(b)}")
            for i in range(min(len(a), len(b))):
                walk(a[i], b[i], f"{prefix}[{i}].")
        else:
            if a != b:
                sa, sb = repr(a), repr(b)
                out.append(f"  [值变了] {path} :: {prefix or '(root)'} "
                           f"v1.2.6={sa[:60]} 现在={sb[:60]}")

    walk(val_o, val_n)


def main() -> int:
    if not OLD_SRC.exists():
        print(f"找不到 v1.2.6 的 worktree：{OLD_SRC}")
        return 1
    uids = real_uids()
    if not uids:
        print("真实库里没有礼物数据，A/B 无意义")
        return 1
    print(f"真实库：{REAL_DB}（{REAL_DB.stat().st_size//1024} KB），主要 uid={uids}")

    sandbox = Path(tempfile.mkdtemp(prefix="ab_"))
    procs = []
    try:
        cfg_old = prepare(sandbox / "old", OLD_SRC)
        cfg_new = prepare(sandbox / "new", ROOT)
        procs.append(start(OLD_SRC, sandbox / "old", cfg_old, PORT_OLD))
        procs.append(start(ROOT, sandbox / "new", cfg_new, PORT_NEW))
        print(f"两版都起来了：v1.2.6=:{PORT_OLD}  现在=:{PORT_NEW}")

        c_old, c_new = Client(PORT_OLD), Client(PORT_NEW)
        c_old.login()
        c_new.login()

        paths = list(SHARED)
        for tpl in UID_TEMPLATES:
            for u in uids:
                paths.append(tpl.format(uid=u))

        regressions: list[str] = []
        new_only: list[str] = []
        same = 0
        for p in paths:
            r_old = c_old.get(p)
            r_new = c_new.get(p)
            if r_old[0] == 404 and r_new[0] == 200:
                new_only.append(p)
                continue
            before = len(regressions)
            diff(p, r_old, r_new, regressions)
            if len(regressions) == before:
                same += 1

        print(f"\n完全一致：{same} / {len(paths)} 个接口")
        if new_only:
            print(f"新版本新增（v1.2.6 是 404）：{len(new_only)} 个")
            for p in new_only[:10]:
                print(f"   + {p}")
        if regressions:
            print(f"\n发现差异 {len(regressions)} 处：")
            for line in regressions:
                print(line)
        else:
            print("\n没有发现任何回归：v1.2.6 有的字段和数值，现在全都在且相同。")

        # 顺手核实「v1.2.6 有、现在故意改掉」的两处（必须能解释清楚）
        print("\n已知的**故意**变更核实：")
        code_o, v_o = c_old.get(f"/api/rooms/{ROOM}/export_all?date_from=&date_to=&gift_type=all&sort=ts")
        code_n, v_n = c_new.get(f"/api/rooms/{ROOM}/export_all?date_from=&date_to=&gift_type=all&sort=ts")
        print(f"  export_all 条数：v1.2.6={len(v_o) if isinstance(v_o, list) else '?'} "
              f"现在={len(v_n) if isinstance(v_n, list) else '?'}（3000 条上限，真实库只有 373 条 → 不触发）")
        code_o, v_o = c_old.get(f"/api/rooms/{ROOM}/ranking?rStart=&rEnd=&rType=all")
        code_n, v_n = c_new.get(f"/api/rooms/{ROOM}/ranking?rStart=&rEnd=&rType=all")
        cnt_o = [r.get("count") for r in (v_o.get("ranking") or [])] if isinstance(v_o, dict) else []
        cnt_n = [r.get("count") for r in (v_n.get("ranking") or [])] if isinstance(v_n, dict) else []
        print(f"  ranking.count：v1.2.6={cnt_o[:4]} 现在={cnt_n[:4]}"
              f"（v1.2.6 是写死的 0，现在给真实条数）")

        print("AB DONE")
        return 0
    finally:
        for p in procs:
            try:
                p.kill()
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
