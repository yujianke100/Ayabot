"""第二十八轮自检：发版脚本（Gitee）的轮换保护、下载地址回读、令牌脱敏。

全部离线：把 `_req` / `list_attachments` / `_head` / `upload` 换成假实现，
不发任何真实请求、不碰令牌。

覆盖：
  1. `rotate()` 永不删除 `latest` 别名的附件（官网/文档/软件内更新都指向它），
     且 latest 不占 keep 名额。
  2. `verify_assets()` 能发现 404 与「大小对不上」（上传成功但文件不对）。
  3. `redact()` 能抹掉 URL 编码后的令牌与 `https://user:token@gitee.com` 形式。
  4. `probe()` 不再往当前目录写文件。

用法: ./.venv/Scripts/python.exe scripts/check_publish_gitee.py
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    pg = _load("pg_under_test", "scripts/publish_gitee.py")
    sg = _load("sg_under_test", "scripts/sync_gitee_repo.py")

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    # ── 1. rotate 保护 latest ──
    print("[1] 轮换不能删掉 latest 别名")
    # latest 故意放在**最老**（列表末尾）：旧实现 releases[keep:] 一定会切到它
    # 8 个真实版本（列表按「新→旧」）+ latest 故意压在最后（最老的位置）
    # 注意 id 必须唯一：给 latest 用 100，避免和 v1.0.1 的 id=1 撞车
    releases = (
        [{"id": i, "tag_name": f"v1.0.{i}"} for i in range(8, 0, -1)]
        + [{"id": 100, "tag_name": "latest"}]
    )
    deleted: list[tuple[str, str]] = []

    def fake_req(method, path, token, *, body=None, files=None, timeout=120):
        if method == "GET" and "/releases?" in path:
            return 200, releases
        if method == "DELETE" and "/attach_files/" in path:
            rel_id = int(path.split("/releases/")[1].split("/")[0])
            att_id = path.rsplit("/", 1)[-1]
            tag = next(r["tag_name"] for r in releases if r["id"] == rel_id)
            deleted.append((tag, att_id))
            return 204, {}
        return 200, {}

    pg._req = fake_req  # type: ignore[assignment]
    pg.list_attachments = lambda t, o, r, rid: [{"id": f"a{rid}", "name": "Ayabot-latest.exe"}]  # type: ignore[assignment]

    pg.rotate("tok", "own", "repo", keep=4)
    tags_deleted = {t for t, _ in deleted}
    ok("latest 的附件没有被删", "latest" not in tags_deleted, tags_deleted)
    ok("keep=4 时删掉 4 个真实版本（8 个版本 - 4）",
       len([t for t in tags_deleted if t != "latest"]) == 4, sorted(tags_deleted))
    ok("保留最新 4 个、删掉最旧 4 个",
       tags_deleted == {"v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4"}, sorted(tags_deleted))
    ok("PROTECTED_TAGS 含 latest", "latest" in pg.PROTECTED_TAGS)

    # 大小写不同的 LATEST 也要保护
    deleted.clear()
    releases2 = [{"id": 200, "tag_name": "LATEST"}] + [
        {"id": 300 + i, "tag_name": f"v2.0.{i}"} for i in range(3, -1, -1)
    ]
    pg._req = lambda method, path, token, **kw: (  # type: ignore[assignment]
        (200, releases2) if method == "GET" and "/releases?" in path
        else (204, {}) if method == "DELETE"
        else (200, {})
    )
    pg.rotate("tok", "own", "repo", keep=1)
    ok("大小写不同的 LATEST 也受保护",
       not any(t.lower() == "latest" for t, _ in deleted), deleted)

    # ── 2. verify_assets ──
    print("[2] 下载地址回读验证")
    tmp = Path(tempfile.mkdtemp(prefix="ayabot_verify_"))
    exe = tmp / "Ayabot-latest.exe"
    exe.write_bytes(b"x" * 1000)

    pg._head = lambda url, timeout=30: (302, 1000)  # type: ignore[assignment]
    ok("地址可用且大小一致 -> True",
       pg.verify_assets("own", "repo", "1.2.7", exe, alias=True) is True)

    pg._head = lambda url, timeout=30: (404, None)  # type: ignore[assignment]
    orig_sleep = pg.time.sleep
    pg.time.sleep = lambda s: None  # type: ignore[assignment]
    ok("404 -> False（上传成功但下载不了）",
       pg.verify_assets("own", "repo", "1.2.7", exe, alias=True) is False)

    pg._head = lambda url, timeout=30: (200, 999)  # type: ignore[assignment]
    ok("大小对不上 -> False（这是别版本的旧文件）",
       pg.verify_assets("own", "repo", "1.2.7", exe, alias=True) is False)

    pg._head = lambda url, timeout=30: (200, None)  # type: ignore[assignment]
    ok("没有 Content-Length 但 200 -> True（只能确认可下载）",
       pg.verify_assets("own", "repo", "1.2.7", exe, alias=True) is True)

    # alias=False 时不该检查 latest
    seen_urls: list[str] = []

    def _head_spy(url, timeout=30):
        seen_urls.append(url)
        return 302, 1000

    pg._head = _head_spy  # type: ignore[assignment]
    pg.verify_assets("own", "repo", "1.2.7", exe, alias=False)
    ok("alias=False 只验版本地址",
       len(seen_urls) == 1 and "/download/latest/" not in seen_urls[0], seen_urls)
    seen_urls.clear()
    pg.verify_assets("own", "repo", "1.2.7", exe, alias=True)
    ok("alias=True 同时验 latest",
       len(seen_urls) == 2 and any("/download/latest/" in u for u in seen_urls), seen_urls)
    pg.time.sleep = orig_sleep  # type: ignore[assignment]

    # ── 3. 令牌脱敏 ──
    print("[3] 日志里的令牌脱敏")
    tok = "abc/DEF+123=xyz"          # 含会被 URL 编码的字符
    encoded = "abc%2FDEF%2B123%3Dxyz"
    line = f"fatal: unable to access 'https://yujianke100:{encoded}@gitee.com/oy/repo.git'"
    out = sg.redact(line, tok)
    ok("原始令牌被抹掉", tok not in out)
    ok("URL 编码后的令牌也被抹掉", encoded not in out, out)
    ok("凭据段整体抹掉", "***@gitee.com" in out, out)
    ok("普通文本不受影响", sg.redact("normal message", tok) == "normal message")
    ok("空令牌不崩", sg.redact("x", "") == "x")

    # ── 4. probe 不写当前目录 ──
    print("[4] probe 的临时文件")
    cwd_before = Path.cwd()
    stray = cwd_before / "probe-ci.txt"
    stray.unlink(missing_ok=True)
    src = (ROOT / "scripts" / "publish_gitee.py").read_text(encoding="utf-8")
    ok("probe 用临时目录", "tempfile.mkdtemp(prefix=\"ayabot_probe_\")" in src)
    ok("不再写 Path(\"probe-ci.txt\")", 'Path("probe-ci.txt")' not in src)
    ok("当前目录没有被写出 probe-ci.txt", not stray.exists())

    # ── 5. 结构断言 ──
    print("[5] 结构断言")
    ok("main 会调用 verify_assets", "verify_assets(" in src.split("def main()")[1])
    ok("验证失败返回非 0", "return 1" in src.split("if not verify_ok:")[1][:200])
    ok("有 --no-verify 开关", '"--no-verify"' in src)
    ok("rotate 过滤受保护 tag", "not in protected" in src)
    ok("受保护 tag 不占 keep 名额", "versioned[keep:]" in src)
    ok("sync 用 redact 而不是裸 replace",
       "redact(line, token)" in (ROOT / "scripts" / "sync_gitee_repo.py").read_text(
           encoding="utf-8"))

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("PUBLISH GITEE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
