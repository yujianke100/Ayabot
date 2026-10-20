"""把某个版本的 exe 发布到 Gitee 发行版（作为国内下载线路）。

为什么是 Gitee：国内直连 GitHub Release 常失败，各家加速站时好时坏；
Gitee 的发行版附件走国内 CDN（foruda.gitee.com），实测 2 MB/s 左右，稳定且不占用自己主机带宽。

配额（社区版）：单个附件 ≤ 100 MB，单仓库附件总量 1 G —— 所以本脚本会轮换，
只保留最近 N 个版本的附件（默认 4 个，约 150 MB）。

用法:
    export GITEE_TOKEN=xxxx                # gitee.com/profile/personal_access_tokens
    python scripts/publish_gitee.py --version 1.2.3 --exe dist/Ayabot-v1.2.3.exe \
        [--notes-file RELEASE.md] [--owner yujianke100] [--repo Ayabot] [--keep 4]

下载地址（发布后稳定可用）：
    https://gitee.com/<owner>/<repo>/releases/download/v<version>/Ayabot-latest.exe
    以及版本无关的固定地址（一个名为 latest 的发行版，每版覆盖）：
    https://gitee.com/<owner>/<repo>/releases/download/latest/Ayabot-latest.exe
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API = "https://gitee.com/api/v5"
ASSET_NAME = "Ayabot-latest.exe"


def _req(method: str, path: str, token: str, *, body: dict | None = None,
         files: dict | None = None, timeout: int = 120) -> tuple[int, dict | list | str]:
    url = f"{API}{path}"
    headers = {"User-Agent": "Ayabot-release", "Accept": "application/json"}
    data = None
    if files:
        # multipart/form-data 上传附件
        boundary = "----ayabot" + uuid.uuid4().hex
        parts: list[bytes] = []
        fields = dict(body or {})
        if token:                       # 空 token 不能带：Gitee 会当成非法令牌返回 401
            fields["access_token"] = token
        for k, v in fields.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
            )
        for k, (fname, content, ctype) in files.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n".encode() + content + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        payload = dict(body)
        if token:
            payload["access_token"] = token
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json;charset=UTF-8"
    elif token:
        sep = "&" if "?" in path else "?"
        url = f"{url}{sep}access_token={urllib.parse.quote(token)}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw


def default_branch(token: str, owner: str, repo: str) -> str:
    """取仓库的默认分支。Gitee 上新仓库可能是 master，也可能是 main —— 硬编码会创建发行版失败。"""
    code, data = _req("GET", f"/repos/{owner}/{repo}", token)
    if code == 200 and isinstance(data, dict):
        branch = str(data.get("default_branch") or "").strip()
        if branch:
            return branch
    return "master"


def get_release_by_tag(token: str, owner: str, repo: str, tag: str) -> dict | None:
    code, data = _req("GET", f"/repos/{owner}/{repo}/releases/tags/{tag}", token)
    if code == 200 and isinstance(data, dict):
        return data
    return None


def ensure_release(token: str, owner: str, repo: str, tag: str, name: str,
                   body: str, target: str) -> dict:
    existing = get_release_by_tag(token, owner, repo, tag)
    if existing:
        print(f"  发行版 {tag} 已存在 (id={existing['id']})，复用")
        # 重发同版本时（例如重打 tag）说明也会变，顺手同步一下，
        # 否则 Gitee 上的介绍会一直停在第一次发布时的内容
        if body and (existing.get("body") or "").strip() != body.strip():
            code, _ = _req("PATCH", f"/repos/{owner}/{repo}/releases/{existing['id']}", token,
                           body={"tag_name": tag, "name": name, "body": body})
            print(f"    更新发行说明 HTTP {code}")
        return existing
    code, data = _req("POST", f"/repos/{owner}/{repo}/releases", token, body={
        "tag_name": tag,
        "name": name,
        "body": body,
        "target_commitish": target,
        "prerelease": False,
    })
    if code not in (200, 201) or not isinstance(data, dict):
        raise SystemExit(
            f"创建 Gitee 发行版失败 (HTTP {code}): {data}\n"
            f"  常见原因：仓库不存在 / token 权限不足 / 目标分支 '{target}' 不存在。\n"
            f"  Gitee 仓库需要至少有一个提交（从 GitHub 导入或 push 一次即可）。"
        )
    print(f"  创建发行版 {tag} (id={data['id']})")
    return data


def list_attachments(token: str, owner: str, repo: str, release_id: int) -> list[dict]:
    code, data = _req("GET", f"/repos/{owner}/{repo}/releases/{release_id}/attach_files?per_page=100", token)
    if code == 200 and isinstance(data, list):
        return data
    return []


def upload(token: str, owner: str, repo: str, release_id: int, exe: Path,
           attempts: int = 4, name: str = ASSET_NAME) -> dict:
    """流式上传附件（不把 38MB 读进内存），失败自动重试。

    为什么不用 urllib 一次性 send：从境外/慢链路传大文件时它会整块写，
    一阻塞就整条失败（实测 GitHub runner → gitee.com 报 "write operation timed out"）。
    这里用 http.client 分块发送，有进度、能重试。
    """
    size = exe.stat().st_size
    print(f"  上传 {name} ({size / 1048576:.1f} MB) ...")
    last_err = ""
    for attempt in range(1, attempts + 1):
        boundary = "----ayabot" + uuid.uuid4().hex
        head = (
            "--" + boundary + "\r\n"
            + 'Content-Disposition: form-data; name="access_token"\r\n\r\n' + token + "\r\n"
            + "--" + boundary + "\r\n"
            + 'Content-Disposition: form-data; name="file"; filename="' + name + '"\r\n'
            + "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        tail = ("\r\n--" + boundary + "--\r\n").encode("utf-8")
        conn = http.client.HTTPSConnection("gitee.com", timeout=180)
        try:
            conn.putrequest(
                "POST", f"/api/v5/repos/{owner}/{repo}/releases/{release_id}/attach_files",
                skip_accept_encoding=True,
            )
            conn.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
            conn.putheader("Content-Length", str(len(head) + size + len(tail)))
            conn.putheader("User-Agent", "Ayabot-release")
            conn.putheader("Accept", "application/json")
            conn.endheaders()
            conn.send(head)
            sent = 0
            step = max(size // 5, 1)
            with exe.open("rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    conn.send(chunk)
                    sent += len(chunk)
                    if sent // step != (sent - len(chunk)) // step:
                        print(f"    {int(sent * 100 / size)}% ({sent / 1048576:.1f} MB)")
            conn.send(tail)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            if resp.status in (200, 201):
                try:
                    data = json.loads(raw)
                except Exception:
                    data = {}
                print(f"  已上传 (id={data.get('id')})")
                return data
            last_err = f"HTTP {resp.status}: {raw[:200]}"
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        print(f"  第 {attempt} 次上传失败：{last_err}")
        if attempt < attempts:
            wait = 5 * attempt
            print(f"  {wait}s 后重试…")
            time.sleep(wait)
    raise SystemExit(f"上传附件失败（已重试 {attempts} 次）：{last_err}")


def _sync_repo(args, token: str) -> int:
    """把本地分支 + tag 推到 Gitee（逻辑都在 sync_gitee_repo.py，避免两份实现）。"""
    script = Path(__file__).resolve().parent / "sync_gitee_repo.py"
    print("同步源码仓库到 Gitee …")
    cmd = [
        sys.executable, str(script),
        "--owner", args.owner, "--repo", args.repo,
    ]
    if getattr(args, "target", ""):
        cmd += ["--branch", args.target]
    try:
        r = subprocess.run(cmd, timeout=900)
    except Exception as exc:  # noqa: BLE001
        print(f"  同步仓库失败（不影响安装包发布）: {exc}")
        return 0
    if r.returncode != 0:
        # 仓库同步失败不能拖累发布结果：包已经传上去了
        print("  同步仓库未完成（不影响安装包发布）")
    return 0


def rotate(token: str, owner: str, repo: str, keep: int) -> None:
    """删除旧版本的附件，避免超过单仓库 1G 的附件上限。"""
    code, releases = _req("GET", f"/repos/{owner}/{repo}/releases?per_page=100", token)
    if code != 200 or not isinstance(releases, list):
        print("  （跳过轮换：读不到发行版列表）")
        return
    for rel in releases[keep:]:
        for att in list_attachments(token, owner, repo, rel["id"]):
            c, _ = _req(
                "DELETE",
                f"/repos/{owner}/{repo}/releases/{rel['id']}/attach_files/{att['id']}", token,
            )
            print(f"  轮换：删除 {rel['tag_name']} / {att.get('name')} -> HTTP {c}")


def _replace_asset(token: str, owner: str, repo: str, release: dict, exe: Path) -> None:
    """同一个发行版里同名附件先删再传（Gitee 不允许重名）。"""
    for att in list_attachments(token, owner, repo, release["id"]):
        if att.get("name") == ASSET_NAME:
            _req("DELETE",
                 f"/repos/{owner}/{repo}/releases/{release['id']}/attach_files/{att['id']}", token)
            print(f"  删除旧附件 (id={att['id']})")
    upload(token, owner, repo, release["id"], exe)


def probe(token: str, owner: str, repo: str, target: str) -> int:
    """最小验证：建一个临时发行版 + 上传 1KB 文件 + 读回确认 + 清理。

    用来判断「令牌是否有效、权限是否够、接口是否可用」，
    与「大文件能不能传（看链路）」分开排查。
    """
    tag = "probe-ci"
    tmp = Path("probe-ci.txt")
    tmp.write_text("ayabot probe " + uuid.uuid4().hex, encoding="utf-8")
    print("① 建临时发行版")
    rel = ensure_release(token, owner, repo, tag, tag, "CI 探测用，可删", target)
    print("② 上传 1KB 文件")
    t0 = time.time()
    att = upload(token, owner, repo, rel["id"], tmp, attempts=1, name="probe-ci.txt")
    print(f"   耗时 {time.time() - t0:.1f}s")
    print("③ 读回附件列表")
    names = [a.get("name") for a in list_attachments(token, owner, repo, rel["id"])]
    print("   attachments:", names)
    ok = "probe-ci.txt" in names
    print("④ 清理")
    if att.get("id"):
        code, _ = _req("DELETE",
                       f"/repos/{owner}/{repo}/releases/{rel['id']}/attach_files/{att['id']}", token)
        print(f"   删附件 HTTP {code}")
    code, _ = _req("DELETE", f"/repos/{owner}/{repo}/releases/{rel['id']}", token)
    print(f"   删发行版 HTTP {code}")
    tmp.unlink(missing_ok=True)
    print("PROBE OK（令牌有效、可建发行版、可上传、可读回、可删除）" if ok
          else "PROBE FAILED：上传后读不到附件")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="", help="版本号，可带 v 前缀（--probe 时可不填）")
    ap.add_argument("--exe", default="", help="本地 exe 路径（--probe 时可不填）")
    ap.add_argument("--notes-file", default="", help="发行说明 Markdown（可选）")
    # 注意：CI 里这些密钥「未配置」时是「已定义但为空」，用 get(k, 默认值) 会拿到空串 →
    # 拼出 /repos//releases 这种 URL，Gitee 直接 405。必须用 `or 默认值`。
    ap.add_argument("--owner", default=os.environ.get("GITEE_OWNER") or "yujianke100")
    ap.add_argument("--repo", default=os.environ.get("GITEE_REPO") or "Ayabot")
    ap.add_argument("--target", default=os.environ.get("GITEE_TARGET") or "",
                    help="发行版指向的分支；留空则自动用仓库默认分支")
    ap.add_argument("--keep", type=int, default=4, help="保留最近几个版本的附件（不含 latest）")
    ap.add_argument("--no-latest-alias", action="store_true",
                    help="不维护 latest 别名发行版（域名/页面就别用固定地址了）")
    ap.add_argument("--prune-only", action="store_true")
    ap.add_argument("--probe", action="store_true", help="只做最小验证（1KB 文件），不发布")
    ap.add_argument("--no-sync-repo", action="store_true",
                    help="发布后不把源码仓库同步到 Gitee（默认会同步）")
    ap.add_argument("--sync-repo-only", action="store_true",
                    help="只同步源码仓库，不发安装包")
    args = ap.parse_args()

    token = os.environ.get("GITEE_TOKEN", "").strip()
    if not token:
        # 本地发布时免去 export：把令牌写进下面任一文件即可（建议 chmod 600）
        for fp in (Path.home() / ".gitee-token",
                   Path.home() / ".config" / "gitee" / "token"):
            if fp.exists():
                token = fp.read_text(encoding="utf-8").strip()
                if token:
                    print(f"（使用 {fp} 里的令牌）")
                    break
    if not token:
        print("未设置 GITEE_TOKEN，也没有 ~/.gitee-token，跳过 Gitee 发布")
        return 0

    if args.probe:
        target = args.target or default_branch(token, args.owner, args.repo)
        print(f"探测 Gitee 权限：{args.owner}/{args.repo}（分支 {target}）")
        return probe(token, args.owner, args.repo, target)

    if args.sync_repo_only:
        return _sync_repo(args, token)

    if not args.version:
        raise SystemExit("缺少 --version")
    ver = args.version.lstrip("v")
    tag = f"v{ver}"
    exe = Path(args.exe)
    if not exe.exists():
        raise SystemExit(f"找不到 exe: {exe}")

    notes = ""
    if args.notes_file and Path(args.notes_file).exists():
        notes = Path(args.notes_file).read_text(encoding="utf-8").strip()
        if len(notes) > 20000:
            notes = notes[:20000] + "\n\n…（完整说明见 GitHub Release）"
    if not notes:
        # Gitee 强校验：描述为空会直接 400「验证失败：发行版的描述不能为空」
        notes = (
            f"Windows 安装包（{tag}），由 GitHub Actions 构建。\n\n"
            f"完整更新说明见 GitHub Release：\n"
            f"https://github.com/yujianke100/Ayabot/releases/tag/{tag}"
        )

    if not args.prune_only:
        target = args.target or default_branch(token, args.owner, args.repo)
        print(f"发布到 Gitee: {args.owner}/{args.repo} {tag}（分支 {target}）")
        rel = ensure_release(token, args.owner, args.repo, tag, tag, notes, target)
        _replace_asset(token, args.owner, args.repo, rel, exe)

        if not args.no_latest_alias:
            # 版本无关的固定地址：官网/文档里写死这个，发版不用改页面
            print("维护 latest 别名发行版（固定下载地址）")
            alias = ensure_release(
                token, args.owner, args.repo, "latest", "最新版本",
                f"始终指向最新版（当前 {tag}）。历史版本见各版本发行版。", target,
            )
            _replace_asset(token, args.owner, args.repo, alias, exe)

    rotate(token, args.owner, args.repo, args.keep)
    print(f"完成：https://gitee.com/{args.owner}/{args.repo}/releases/download/{tag}/{ASSET_NAME}")
    print(f"固定：https://gitee.com/{args.owner}/{args.repo}/releases/download/latest/{ASSET_NAME}")

    # 安装包发完了，再把源码仓库也推一遍：Gitee 上的代码原来靠「仓库镜像」拉，
    # 受 GitHub 可达性影响会落后，出现「包是新的、代码还是旧的」。这里直接推，不靠镜像。
    if not args.no_sync_repo:
        _sync_repo(args, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
