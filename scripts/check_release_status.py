"""等待 v1.2.0 的 CI 构建完成并核对 Release 资产（未鉴权 REST，无需 gh 登录）。

用法: ./.venv/Scripts/python.exe scripts/check_release_status.py v1.2.0 [--wait 秒]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

OWNER_REPO = "yujianke100/ayabot"


def api(path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{OWNER_REPO}{path}",
        headers={"User-Agent": "ayabot-release-check", "Accept": "application/vnd.github+json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def main(tag: str, wait_s: int) -> int:
    sha_expected = ""
    runs_url = "/actions/runs?per_page=8"
    deadline = time.time() + wait_s
    last = ""
    while True:
        runs = api(runs_url)["workflow_runs"]
        mine = [r for r in runs if r["head_branch"] == tag]
        exe = next((r for r in mine if "exe" in r["name"].lower()), None)
        dkr = next((r for r in mine if "docker" in r["name"].lower()), None)
        if exe:
            sha_expected = exe["head_sha"]
            state = (f'exe {exe["head_sha"][:7]} {exe["status"]}/{exe.get("conclusion")} | '
                     f'docker {dkr["status"] + "/" + str(dkr.get("conclusion")) if dkr else "-"}')
            if state != last:
                print(f"[{time.strftime('%H:%M:%S')}] {state}", flush=True)
                last = state
            if exe["status"] == "completed":
                if exe.get("conclusion") != "success":
                    print("FAILED: exe 构建未成功")
                    return 1
                break
        if time.time() > deadline:
            print("TIMEOUT: CI 未在限定时间内完成")
            return 2
        time.sleep(15)

    rel = api(f"/releases/tags/{tag}")
    print(f"\nrelease {rel['tag_name']} published={rel['published_at']} updated={rel.get('updated_at')}")
    names = {}
    for a in rel["assets"]:
        names[a["name"]] = a
        print(f'  {a["name"]:24s} {a["size"]:>10} bytes  dl={a["download_count"]}  {a["updated_at"]}')
    ok = True
    if "Ayabot-latest.exe" not in names:
        print("FAILED: 缺少 Ayabot-latest.exe（国内镜像与检查更新都依赖它）")
        ok = False
    body = (rel.get("body") or "").strip()
    dup = body.count("Release notes generated")
    print(f"\nrelease body: {len(body)} chars, 自动生成块 {dup} 个")
    if dup > 1:
        print("注意: 正文里有重复的自动生成块（多次重打 tag 导致），需要手动清理")
    print("\nOK" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "v1.2.0"
    wait = 420
    if "--wait" in sys.argv:
        wait = int(sys.argv[sys.argv.index("--wait") + 1])
    raise SystemExit(main(tag, wait))
