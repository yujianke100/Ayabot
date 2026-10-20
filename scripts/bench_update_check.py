"""纯国内网络（无代理）下对比新旧更新检查逻辑的耗时。

old: 串行试元数据（GitHub 直连优先，8s 超时）+ gather 全部线路（10s 超时）
new: 并发抢答元数据（国内镜像先跑）+ 拿到可用线路后 0.5s 收工 + 缓存
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

API = "https://api.github.com/repos/yujianke100/ayabot/releases/latest"
ASSET = "https://github.com/yujianke100/ayabot/releases/download/v1.2.1/Ayabot-latest.exe"  # 运行时会被替换

OLD_META = [("GitHub", ""), ("gh-proxy.com", "https://gh-proxy.com/"),
            ("gh-proxy.net", "https://gh-proxy.net/")]
OLD_DL = [("GitHub", ""), ("gh-proxy.com", "https://gh-proxy.com/"),
          ("gh.llkk.cc", "https://gh.llkk.cc/"),
          ("ghfile.geekertao.top", "https://ghfile.geekertao.top/"),
          ("ghfast.top", "https://ghfast.top/"), ("ghproxy.net", "https://ghproxy.net/")]


async def old_logic(session, asset: str) -> tuple[float, str, list]:
    t0 = time.time()
    src = ""
    for name, prefix in OLD_META:
        try:
            async with session.get(prefix + API, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    continue
                await r.json(content_type=None)
            src = name or "GitHub"
            break
        except Exception:
            continue
    probes = await asyncio.gather(*[
        _probe(session, n or "GitHub", p + asset, 10) for n, p in OLD_DL
    ], return_exceptions=True)
    ok = [p for p in probes if isinstance(p, dict) and p["ok"]]
    return time.time() - t0, src, ok


async def _probe(session, name, url, timeout):
    t = time.time()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                               headers={"Range": "bytes=0-0"}) as r:
            size = r.headers.get("Content-Range") or ""
        return {"name": name, "ok": r.status < 400, "ms": int((time.time() - t) * 1000), "size": size}
    except Exception as e:
        return {"name": name, "ok": False, "ms": int((time.time() - t) * 1000), "err": type(e).__name__}


async def new_logic(session) -> tuple[float, str, list]:
    from app.web import server as srv

    t0 = time.time()
    meta, src, meta_ms = await srv._fetch_meta_race(session)
    if not meta:
        return time.time() - t0, "（失败）", []
    latest = str(meta.get("tag_name") or "").lstrip("vV")
    urls = srv._release_urls(latest)
    expected = 0
    for a in meta.get("assets") or []:
        if a.get("name") == srv._UPDATE_ASSET:
            expected = int(a.get("size") or 0)
    probes = await srv._probe_all(session, urls["asset"], expected)
    ok = [p for p in probes if p["ok"]]
    return time.time() - t0, f"{src} ({meta_ms}ms)", ok


async def main() -> None:
    hdr = {"User-Agent": "Ayabot-UpdateChecker", "Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession(headers=hdr, trust_env=True) as s:
        # 用真实存在的 latest tag 拼资产地址，避免 404 被误读成「线路不通」
        async with s.get(API + "", timeout=aiohttp.ClientTimeout(total=10)) as r:
            tag = json.loads(await r.read())["tag_name"]
        asset = f"https://github.com/yujianke100/ayabot/releases/download/{tag}/Ayabot-latest.exe"
        print(f"资产地址: {asset}\n")
        print("== 旧逻辑（v1.2.1 及更早）==")
        t, src, ok = await old_logic(s, asset)
        print(f"   耗时 {t:.2f}s | 版本号来源 {src} | 可用线路 {[(p['name'], p['ms']) for p in ok]}")

        print("\n== 新逻辑（v1.2.2）==")
        for i in range(3):
            t2, src2, ok2 = await new_logic(s)
            print(f"   第{i+1}次 耗时 {t2:.2f}s | 版本号来源 {src2} | 可用线路 {[(p['name'], p['ms']) for p in ok2][:4]}")

        print("\n== 各线路单独探测（并发，10s 超时，看国内网络下谁挂死）==")
        res = await asyncio.gather(*[_probe(s, n or "GitHub", p + asset, 10) for n, p in OLD_DL])
        for r in sorted(res, key=lambda x: x["ms"]):
            state = "OK" if r["ok"] else f"FAIL {r.get('err') or ''}"
            print(f"   {r['name']:22} {r['ms']:6}ms {state} {r.get('size','')[:28]}")

        print("\n== 元数据源单独探测（8s 超时）==")
        for n, p in OLD_META + [("hk.gh-proxy.com", "https://hk.gh-proxy.com/")]:
            st = time.time()
            try:
                async with s.get(p + API, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    raw = await r.read()
                try:
                    tag = json.loads(raw).get("tag_name")
                    note = f"JSON tag={tag}"
                except Exception:
                    note = f"非JSON {raw[:40]!r}"
                print(f"   {n or 'GitHub':22} {int((time.time()-st)*1000):6}ms HTTP {r.status} {note}")
            except Exception as e:
                print(f"   {n or 'GitHub':22} {int((time.time()-st)*1000):6}ms ERR {type(e).__name__}")


asyncio.run(main())
