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
import json
import os
import sys
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
        payload["access_token"] = token
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json;charset=UTF-8"
    else:
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


def upload(token: str, owner: str, repo: str, release_id: int, exe: Path) -> dict:
    content = exe.read_bytes()
    print(f"  上传 {exe.name} ({len(content) / 1048576:.1f} MB) ...")
    code, data = _req(
        "POST", f"/repos/{owner}/{repo}/releases/{release_id}/attach_files", token,
        body={}, files={"file": (ASSET_NAME, content, "application/octet-stream")},
        timeout=600,
    )
    if code not in (200, 201) or not isinstance(data, dict):
        raise SystemExit(f"上传附件失败 (HTTP {code}): {data}")
    print(f"  已上传 (id={data.get('id')})")
    return data


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="版本号，可带 v 前缀")
    ap.add_argument("--exe", required=True, help="本地 exe 路径")
    ap.add_argument("--notes-file", default="", help="发行说明 Markdown（可选）")
    ap.add_argument("--owner", default=os.environ.get("GITEE_OWNER", "yujianke100"))
    ap.add_argument("--repo", default=os.environ.get("GITEE_REPO", "Ayabot"))
    ap.add_argument("--target", default=os.environ.get("GITEE_TARGET", "master"),
                    help="发行版指向的分支（Gitee 上要存在）")
    ap.add_argument("--keep", type=int, default=4, help="保留最近几个版本的附件（不含 latest）")
    ap.add_argument("--no-latest-alias", action="store_true",
                    help="不维护 latest 别名发行版（域名/页面就别用固定地址了）")
    ap.add_argument("--prune-only", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("GITEE_TOKEN", "").strip()
    if not token:
        print("未设置 GITEE_TOKEN，跳过 Gitee 发布")
        return 0

    ver = args.version.lstrip("v")
    tag = f"v{ver}"
    exe = Path(args.exe)
    if not exe.exists():
        raise SystemExit(f"找不到 exe: {exe}")

    notes = ""
    if args.notes_file and Path(args.notes_file).exists():
        notes = Path(args.notes_file).read_text(encoding="utf-8")
        if len(notes) > 20000:
            notes = notes[:20000] + "\n\n…（完整说明见 GitHub Release）"

    if not args.prune_only:
        print(f"发布到 Gitee: {args.owner}/{args.repo} {tag}")
        rel = ensure_release(token, args.owner, args.repo, tag, tag, notes, args.target)
        _replace_asset(token, args.owner, args.repo, rel, exe)

        if not args.no_latest_alias:
            # 版本无关的固定地址：官网/文档里写死这个，发版不用改页面
            print("维护 latest 别名发行版（固定下载地址）")
            alias = ensure_release(
                token, args.owner, args.repo, "latest", "最新版本",
                f"始终指向最新版（当前 {tag}）。历史版本见各版本发行版。", args.target,
            )
            _replace_asset(token, args.owner, args.repo, alias, exe)

    rotate(token, args.owner, args.repo, args.keep)
    print(f"完成：https://gitee.com/{args.owner}/{args.repo}/releases/download/{tag}/{ASSET_NAME}")
    print(f"固定：https://gitee.com/{args.owner}/{args.repo}/releases/download/latest/{ASSET_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
