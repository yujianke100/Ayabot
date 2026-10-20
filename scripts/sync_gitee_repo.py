"""把本地仓库（分支 + 全部 tag）同步到 Gitee，让源码备份与 GitHub 保持一致。

为什么需要它：Gitee 上的代码靠「仓库镜像」拉取，受 GitHub 侧可达性影响，经常落后甚至停了；
而安装包是我们自己用 API 推上去的，所以会出现「发行版有最新包、代码还停在旧提交」的怪状态。
这个脚本直接用 token 推送，不依赖 Gitee 能不能连上 GitHub。

令牌优先级：--token > 环境变量 GITEE_TOKEN > ~/.gitee-token
令牌**不会**写进 .git/config，也不会新增 remote（每次调用现拼 URL）。

用法:
    python scripts/sync_gitee_repo.py                  # 同步当前分支 + 全部 tag
    python scripts/sync_gitee_repo.py --branch main    # 指定分支
    python scripts/sync_gitee_repo.py --verify-only    # 只比对，不推送
    python scripts/sync_gitee_repo.py --tags-only      # 只补 tag
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://gitee.com/api/v5"
DEFAULT_OWNER = "yujianke100"
DEFAULT_REPO = "Ayabot"


def read_token(explicit: str = "") -> str:
    token = (explicit or "").strip()
    if token:
        return token
    token = (os.environ.get("GITEE_TOKEN") or "").strip()
    if token:
        return token
    for fp in (Path.home() / ".gitee-token",):
        try:
            t = fp.read_text(encoding="utf-8").strip()
            if t:
                return t
        except OSError:
            continue
    return ""


def git(*args: str, cwd: Path, url: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"          # 绝不卡在交互式账号密码上
    cmd = ["git", *args]
    if url and "push" in args:
        cmd.append(url)
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)


def local_sha(branch: str, cwd: Path) -> str:
    r = git("rev-parse", branch, cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else ""


def api_get(path: str, token: str) -> dict | list | None:
    sep = "&" if "?" in path else "?"
    url = f"{API}{path}{sep}access_token={urllib.parse.quote(token)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Ayabot-gitee-sync"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:160]
        print(f"  API {path} -> HTTP {exc.code} {detail}")
    except Exception as exc:  # noqa: BLE001
        print(f"  API {path} -> {exc}")
    return None


def remote_sha(owner: str, repo: str, branch: str, token: str) -> str:
    data = api_get(f"/repos/{owner}/{repo}/branches/{urllib.parse.quote(branch)}", token)
    if isinstance(data, dict):
        return str((data.get("commit") or {}).get("sha") or "")
    return ""


def remote_tags(owner: str, repo: str, token: str) -> set[str]:
    data = api_get(f"/repos/{owner}/{repo}/tags", token)
    if isinstance(data, list):
        return {str(t.get("name") or "") for t in data if isinstance(t, dict)}
    return set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default=os.environ.get("GITEE_OWNER") or DEFAULT_OWNER)
    ap.add_argument("--repo", default=os.environ.get("GITEE_REPO") or DEFAULT_REPO)
    ap.add_argument("--branch", default="", help="要同步的分支，默认当前分支")
    ap.add_argument("--token", default="")
    ap.add_argument("--verify-only", action="store_true", help="只比对本地与 Gitee，不推送")
    ap.add_argument("--tags-only", action="store_true", help="只推 tag，不推分支")
    ap.add_argument("--repo-dir", default="", help="仓库目录，默认脚本所在仓库")
    args = ap.parse_args()

    cwd = Path(args.repo_dir).resolve() if args.repo_dir else Path(__file__).resolve().parent.parent
    token = read_token(args.token)
    if not token and not args.verify_only:
        print("缺少 Gitee 令牌：export GITEE_TOKEN=xxx 或写入 ~/.gitee-token")
        return 2

    branch = args.branch
    if not branch:
        r = git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
        branch = r.stdout.strip() or "main"

    lsha = local_sha(branch, cwd)
    if not lsha:
        print(f"本地找不到分支 {branch}")
        return 1
    rsha = remote_sha(args.owner, args.repo, branch, token) if token else ""
    print(f"分支 {branch}: 本地 {lsha[:7]} / Gitee {rsha[:7] or '（读取失败）'}")

    tags_local = set()
    r = git("tag", cwd=cwd)
    if r.returncode == 0:
        tags_local = {t.strip() for t in r.stdout.splitlines() if t.strip()}
    tags_remote = remote_tags(args.owner, args.repo, token) if token else set()
    missing_tags = sorted(tags_local - tags_remote)
    if missing_tags:
        print(f"Gitee 缺少 {len(missing_tags)} 个 tag: {', '.join(missing_tags[:6])}"
              + (" …" if len(missing_tags) > 6 else ""))

    if args.verify_only:
        same = rsha == lsha
        print("结论:", "已同步" if same and not missing_tags else "有差异")
        return 0 if same and not missing_tags else 1

    if lsha == rsha and not missing_tags and not args.tags_only:
        print("代码已是最新，无需推送")
        return 0

    # 令牌随 URL 现拼，不写进 .git/config，也不新增 remote
    auth_url = f"https://{args.owner}:{urllib.parse.quote(token, safe='')}@gitee.com/{args.owner}/{args.repo}.git"

    if not args.tags_only:
        print(f"推送分支 {branch} …")
        r = git("push", auth_url, f"{branch}:{branch}", cwd=cwd)
        out = (r.stdout + r.stderr).strip()
        for line in out.splitlines():
            if line.strip():
                print("   ", line.replace(token, "***"))
        if r.returncode != 0:
            print("推送分支失败")
            return 1

    if tags_local:
        print(f"推送 {len(tags_local)} 个 tag …")
        r = git("push", "--force", auth_url, "--tags", cwd=cwd)
        out = (r.stdout + r.stderr).strip()
        for line in out.splitlines()[-4:]:
            if line.strip():
                print("   ", line.replace(token, "***"))
        # tag 推送失败不算致命（例如受保护 tag），但要提示
        if r.returncode != 0:
            print("    部分 tag 推送失败（分支已同步）")

    rsha2 = remote_sha(args.owner, args.repo, branch, token)
    tags_remote2 = remote_tags(args.owner, args.repo, token)
    ok = rsha2 == lsha
    print(f"校验: Gitee {branch} = {rsha2[:7] or '?'} → {'一致' if ok else '不一致'}")
    if tags_remote2:
        left = tags_local - tags_remote2
        print(f"       tag 共 {len(tags_remote2 & tags_local)}/{len(tags_local)} 已同步"
              + (f"（缺 {len(left)}）" if left else ""))
    print("SYNC OK" if ok else "SYNC FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
