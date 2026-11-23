"""第二十九轮自检：部署面（Docker / compose / 打包入口）的不变量。

覆盖的问题：
  1. `.dockerignore` 必须排除 `accounts/` 与 `rooms/` —— Dockerfile 是 `COPY . .`，
     漏掉这两项会把用户的 B站 Cookie 与观众数据打进镜像层。
  2. `docker-compose.yml` 里每个带 `build:` 的服务，其上下文目录必须真实存在
     （README 的安装步骤就是 `docker compose up -d`，一个坏服务会让整条命令失败）。
  3. README 里写的 `wget <文件>` 必须都是仓库里真实存在的文件。
  4. `web_serve.py` 的 `--port` 必须真的生效（容器里靠它/环境变量换端口）。

用法: ./.venv/Scripts/python.exe scripts/check_deploy_files.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    import yaml

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}"
              + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    # ── 1. .dockerignore ──
    print("[1] .dockerignore 必须挡住用户私有数据")
    lines = [
        ln.strip() for ln in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    ]
    active = [ln for ln in lines if ln and not ln.startswith("#")]
    for need in ("accounts/", "rooms/", "data/", "config.yaml", "*.db", ".git/"):
        ok(f"排除了 {need}", need in active, active)

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    ok("Dockerfile 确实是 COPY . .（所以 .dockerignore 才有意义）",
       "COPY . ." in dockerfile)

    # ── 2. compose 的 build 上下文必须存在 ──
    print("[2] compose 里每个 build 上下文都得真实存在")
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose.get("services") or {}
    ok("默认 compose 至少有 ayabot", "ayabot" in services, list(services))
    for name, svc in services.items():
        build = (svc or {}).get("build")
        if not build:
            continue
        ctx = build if isinstance(build, str) else build.get("context", ".")
        exists = (ROOT / ctx).exists()
        ok(f"{name} 的 build 上下文存在（{ctx}）", exists,
           "目录不存在 → docker compose up 会失败")

    # 可选附加文件：单独存在，且引用的目录可以不存在（它本来就是可选的）
    opt = ROOT / "docker-compose.t2i.yml"
    ok("可选 t2i 已拆到单独文件", opt.exists())
    if opt.exists():
        opt_cfg = yaml.safe_load(opt.read_text(encoding="utf-8"))
        ok("单独文件里的服务不会影响默认启动",
           "t2i-local" in (opt_cfg.get("services") or {}))

    # 默认 compose 里不能出现指向不存在目录的 build
    bad = []
    for name, svc in services.items():
        build = (svc or {}).get("build")
        if not build:
            continue
        ctx = build if isinstance(build, str) else build.get("context", ".")
        if not (ROOT / ctx).exists():
            bad.append(f"{name}->{ctx}")
    ok("默认 compose 没有坏 build 上下文", not bad, bad)

    # ── 3. README 里引用的文件必须存在 ──
    print("[3] README 里的安装命令引用真实文件")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    refs = set(re.findall(r"raw\.githubusercontent\.com/[\w.-]+/[\w.-]+/[\w.-]+/([\w./-]+)", readme))
    refs |= set(re.findall(r"wget[^\n]*?/([\w.-]+\.(?:yml|yaml|sh|py))", readme))
    ok("README 至少引用了一个仓库文件", bool(refs), sorted(refs))
    for ref in sorted(refs):
        p = ROOT / ref
        ok(f"README 引用的 {ref} 存在", p.exists())

    # ── 4. web_serve.py 的端口参数 ──
    print("[4] web_serve.py 端口优先级")
    src = (ROOT / "web_serve.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    ok("支持 --port", '"--port"' in src)
    ok("--port 优先于环境变量", "args.port or int(os.environ.get(\"AYABOT_PORT\"" in src)
    ok("支持 --host", '"--host"' in src)

    # 真跑一次 --help，确认参数能解析（不要起服务）
    proc = subprocess.run([sys.executable, "web_serve.py", "--help"],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    ok("--help 能正常打印", proc.returncode == 0 and "--port" in proc.stdout,
       (proc.stdout or proc.stderr)[:120])

    # ── 5. Dockerfile 关键项 ──
    print("[5] Dockerfile")
    ok("暴露 19810", "EXPOSE 19810" in dockerfile)
    ok("数据卷包含 rooms 与 accounts",
       '"/app/rooms"' in dockerfile and '"/app/accounts"' in dockerfile)
    ok("入口是 web_serve.py", 'CMD ["python", "web_serve.py"]' in dockerfile)
    ok("用 slim 基础镜像（体积）", "python:3.13-slim" in dockerfile)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项:")
        for f in fails:
            print("  -", f)
        return 1
    print("DEPLOY FILES OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
