"""核对「当前 VERSION」对应的 GitHub 发行版是否已经发全（未鉴权 REST，无需 gh 登录）。

用法:
    ./.venv/Scripts/python.exe scripts/check_release_status.py            # 按 VERSION 单次核对
    ./.venv/Scripts/python.exe scripts/check_release_status.py v1.2.7     # 核对指定 tag
    ./.venv/Scripts/python.exe scripts/check_release_status.py v1.2.7 --wait 420   # 等 CI 跑完

它是**发版工具**，但同时被 run_all_checks 当成门禁调用，所以：
- 不给 tag 时按本地 `VERSION` 判断，而不是写死 `v1.2.0`（写死的老默认值会让门禁
  空等 7 分钟再报 TIMEOUT，看起来像产品坏了）；
- 单次模式必须在几秒内给出结论：网络异常报「网络错误」而不是挂住；
- 只有显式 `--wait` 才进入轮询。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OWNER_REPO = "yujianke100/ayabot"
ROOT = Path(__file__).resolve().parent.parent


def api(path: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{OWNER_REPO}{path}",
        headers={"User-Agent": "ayabot-release-check", "Accept": "application/vnd.github+json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def local_version() -> str:
    raw = (ROOT / "VERSION").read_text(encoding="utf-8-sig").strip()
    return raw.lstrip("vV")


def check_release(tag: str) -> int:
    """核对 tag 对应发行版的资产完整性。返回 0/1/2（2=网络不通，不是产品问题）。"""
    try:
        rel = api(f"/releases/tags/{tag}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"FAILED: GitHub 上没有 {tag} 这个发行版（还没发？）")
            return 1
        print(f"FAILED: 查询 {tag} 失败 HTTP {exc.code}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"网络错误，无法查询 GitHub（{type(exc).__name__}: {exc}）")
        return 2

    print(f"release {rel['tag_name']} published={rel.get('published_at')} updated={rel.get('updated_at')}")
    names: dict[str, dict] = {}
    for a in rel.get("assets") or []:
        names[a["name"]] = a
        print(f'  {a["name"]:24s} {a["size"]:>10} bytes  dl={a["download_count"]}  {a["updated_at"]}')

    ok = True
    if "Ayabot-latest.exe" not in names:
        print("FAILED: 缺少 Ayabot-latest.exe（国内镜像与检查更新都依赖它）")
        ok = False
    versioned = f"Ayabot-{tag}.exe"
    if versioned not in names:
        print(f"FAILED: 缺少版本资产 {versioned}")
        ok = False
    # 两个资产必须同字节数，否则「检查更新」算出的 expected_size 和实际下载对不上，
    # 各条下载线路会被误判成 stale
    if "Ayabot-latest.exe" in names and versioned in names:
        a, b = names["Ayabot-latest.exe"]["size"], names[versioned]["size"]
        if a != b:
            print(f"FAILED: latest({a}) 与 {versioned}({b}) 字节数不一致")
            ok = False

    body = (rel.get("body") or "").strip()
    dup = body.count("Release notes generated")
    print(f"\nrelease body: {len(body)} chars, 自动生成块 {dup} 个")
    if dup > 1:
        print("注意: 正文里有重复的自动生成块（多次重打 tag 导致），需要手动清理")

    print("\nOK" if ok else "\nFAILED")
    return 0 if ok else 1


def wait_ci(tag: str, wait_s: int) -> str:
    """轮询 CI，直到该 tag 的 exe 构建结束。成功返回 sha，失败/超时返回空串。"""
    deadline = time.time() + wait_s
    last = ""
    while True:
        try:
            runs = api("/actions/runs?per_page=10")["workflow_runs"]
        except Exception as exc:  # noqa: BLE001
            print(f"（查询 CI 失败，重试：{type(exc).__name__}）", flush=True)
            runs = []
        mine = [r for r in runs if r.get("head_branch") == tag]
        exe = next((r for r in mine if "exe" in r["name"].lower()), None)
        dkr = next((r for r in mine if "docker" in r["name"].lower()), None)
        if exe:
            state = (f'exe {exe["head_sha"][:7]} {exe["status"]}/{exe.get("conclusion")} | '
                     f'docker {dkr["status"] + "/" + str(dkr.get("conclusion")) if dkr else "-"}')
            if state != last:
                print(f"[{time.strftime('%H:%M:%S')}] {state}", flush=True)
                last = state
            if exe["status"] == "completed":
                return exe["head_sha"] if exe.get("conclusion") == "success" else ""
        if time.time() > deadline:
            return ""
        time.sleep(15)


def main(tag: str, wait_s: int) -> int:
    if not tag:
        tag = f"v{local_version()}"
        print(f"（未指定 tag，按本地 VERSION 用 {tag}）")
    if wait_s > 0:
        if not wait_ci(tag, wait_s):
            print("FAILED: CI 未在限定时间内成功完成")
            return 2
    return check_release(tag)


if __name__ == "__main__":
    args = list(sys.argv[1:])
    wait = 0
    if "--wait" in args:
        i = args.index("--wait")
        wait = int(args[i + 1]) if len(args) > i + 1 else 420
        del args[i:i + 2]
    raise SystemExit(main(args[0] if args else "", wait))
