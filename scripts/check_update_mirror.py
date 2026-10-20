"""更新检查接口验证：能取到版本、能探测出可用下载镜像。

用法: ./.venv/Scripts/python.exe scripts/check_update_mirror.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 19897
TOKEN = "test-session-token"


def _get(path: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", headers={"Cookie": f"session={TOKEN}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def main() -> int:
    import uvicorn

    from app.config import load_config
    from app.web import server as srv

    cfg_path = str(ROOT / "rooms" / "1946287911" / "config.yaml")
    srv.init_app(load_config(cfg_path), config_path=cfg_path)
    srv._SESSIONS[TOKEN] = (time.time() + 600, "tester", "admin", [])

    server = uvicorn.Server(uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.5)
    if not server.started:
        print("FAILED: uvicorn 未启动")
        return 1

    failures: list[str] = []
    try:
        started = time.time()
        code, data = _get("/api/update/check?force=1")
        elapsed = time.time() - started
        print(f"HTTP {code} in {elapsed:.2f}s（force=1 真测速）")
        print(json.dumps({k: v for k, v in data.items() if k != "changelog"}, ensure_ascii=False, indent=1)[:2200])

        if code != 200:
            failures.append(f"/api/update/check -> {code}")
        if not data.get("ok"):
            failures.append(f"未取到版本信息: {data.get('error')}")
        if not data.get("latest"):
            failures.append("缺少 latest 版本号")

        # ① 版本号必须来自国内线路（GitHub 直连国内常挂死，不能作为首选）
        print("meta_source:", data.get("meta_source"), "| meta_ms:", data.get("meta_ms"))
        if int(data.get("meta_ms") or 0) <= 0:
            failures.append("缺少 meta_ms（无法判断取版本号耗时）")
        elif int(data.get("meta_ms") or 0) > 2500:
            failures.append(f"取版本号耗时 {data['meta_ms']}ms，超过 2.5s")
        # 静态断言：两个候选表都必须把 GitHub 直连排在最后
        if srv._META_SOURCES[-1][1] != "":
            failures.append("_META_SOURCES 未把 GitHub 直连排在最后")
        if srv._DOWNLOAD_SOURCES[-1][1] != "":
            failures.append("_DOWNLOAD_SOURCES 未把 GitHub 直连排在最后")
        if not any(p for _, p in srv._META_SOURCES[:-1]):
            failures.append("_META_SOURCES 缺少国内镜像候选")

        # ② 总耗时要短：旧实现串行等 GitHub 超时 + 等最慢线路，实测 10~18s
        if elapsed > 5.0:
            failures.append(f"检查更新耗时 {elapsed:.1f}s，超过 5s 预算")

        # ③ 缓存：同样的请求第二次必须命中缓存、几乎瞬间返回
        t2 = time.time()
        code_c, data_c = _get("/api/update/check")
        elapsed_c = time.time() - t2
        print(f"缓存命中: {elapsed_c * 1000:.0f}ms cached={data_c.get('cached')}")
        if not data_c.get("cached"):
            failures.append("第二次请求没有命中缓存")
        if elapsed_c > 0.5:
            failures.append(f"缓存响应 {elapsed_c:.2f}s，应该几乎瞬时")

        downloads = data.get("downloads") or []
        ok_ones = [d for d in downloads if d.get("ok")]
        print("\n可用线路:", [(d["name"], d.get("ms")) for d in ok_ones])
        if not ok_ones:
            failures.append("没有任何可用下载线路（含直连与镜像）")
        else:
            first = downloads[0]
            if not first.get("ok"):
                failures.append("recommended 未排在第一个可用线路")
            # 至少应探测到国内镜像（GitHub 直连在国内常不可用）
            if not any(d["name"] != "GitHub" and d.get("ok") for d in ok_ones):
                failures.append("国内镜像均不可用")
            # 推荐线路必须是「可用且不是旧缓存」
            rec = data.get("recommended") or {}
            print("recommended:", rec.get("name"), "stale=", rec.get("stale"),
                  "bytes=", rec.get("total_bytes"), "expected=", data.get("expected_bytes"))
            if rec.get("stale"):
                failures.append(f"推荐了缓存旧文件的线路: {rec.get('name')}")
            if rec.get("name") == "GitHub" and any(
                d["name"] != "GitHub" and d.get("ok") and not d.get("stale") for d in ok_ones
            ):
                failures.append("有可用国内镜像时却推荐了 GitHub 直连")
            stale = [d["name"] for d in downloads if d.get("stale")]
            if stale:
                print("检测到缓存旧版的线路:", stale)
        if data.get("ok") and data.get("has_update") and not data.get("recommended"):
            failures.append("有待更新时未给出推荐下载地址")

        # 模拟旧版本客户端（v1.1.1）→ 应当看到 v1.2.0 的更新
        orig = srv.get_version_display
        srv.get_version_display = lambda: "v1.1.1"  # type: ignore[assignment]
        try:
            srv._UPDATE_CACHE["ts"] = 0.0  # 清缓存，重新走一遍真实检查
            code2, old_data = _get("/api/update/check?force=1")
            print("\n模拟 v1.1.1 客户端:", json.dumps(
                {k: old_data.get(k) for k in ("ok", "current", "latest", "has_update")},
                ensure_ascii=False))
            if code2 != 200 or not old_data.get("ok"):
                failures.append("旧版本客户端检查更新失败")
            if not old_data.get("has_update"):
                failures.append("v1.1.1 客户端未识别出 v1.2.0 更新")
        finally:
            srv.get_version_display = orig  # type: ignore[assignment]
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nUPDATE MIRROR OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
