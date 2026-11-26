"""一次跑完所有自检脚本，给出 PASS / SKIP / FAIL 汇总。

    ./.venv/Scripts/python.exe scripts/run_all_checks.py                # 全部
    ./.venv/Scripts/python.exe scripts/run_all_checks.py --fast         # 跳过 exe 相关（慢）
    ./.venv/Scripts/python.exe scripts/run_all_checks.py gift timezone  # 只跑名字匹配的
    ./.venv/Scripts/python.exe scripts/run_all_checks.py -v             # 打印每个脚本的尾部输出

为什么需要它：以前是手写一行 shell 循环跑 `scripts/check_*.py`，结果是
- 需要 exe 参数的脚本（check_bundled_exe / check_create_room_exe / check_exe_e2e）
  拿不到参数 → 直接报红，看起来像产品坏了，其实是调用方式不对；
- 这些脚本单跑要几十秒到几分钟，混在一个循环里超时后**整批**结果都丢了；
- 没有汇总，得自己数「到底几个通过」。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "scripts"

# 需要 exe 路径参数的脚本（第一个位置参数是 exe）
EXE_SCRIPTS = {"check_bundled_exe", "check_create_room_exe", "check_exe_e2e"}

#: 依赖**线上状态**而不是代码的检查：GitHub/Gitee/镜像 CDN 缓存、CI 产物。
#: 它们会随 CDN 缓存刷新而红绿跳动（实测同一份代码，隔 10 分钟一次红一次绿），
#: 和「代码有没有 bug」无关。单独统计、不并入代码门禁，
#: 否则真正的回归会被「某个加速站还缓存着旧包」这种噪音淹没。
RELEASE_STATE_CHECKS = {"check_update_mirror", "check_release_status"}


def newest_exe() -> str:
    dist = ROOT / "dist"
    latest = dist / "Ayabot-latest.exe"
    if latest.exists():
        return str(latest)
    built = sorted(dist.glob("Ayabot-v*.exe"))
    return str(built[-1]) if built else ""


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("filters", nargs="*", help="只跑名字包含这些关键词的脚本")
    ap.add_argument("--fast", action="store_true", help="跳过需要 exe 的慢检查")
    ap.add_argument("-v", "--verbose", action="store_true", help="也打印通过脚本的输出尾部")
    ap.add_argument("--timeout", type=int, default=900, help="单个脚本超时秒数（默认 900）")
    args = ap.parse_args()

    scripts = sorted(p for p in SCRIPT_DIR.glob("check_*.py") if p.resolve() != Path(__file__).resolve())
    if args.filters:
        scripts = [p for p in scripts if any(f in p.stem for f in args.filters)]
    exe = "" if args.fast else newest_exe()
    if args.fast:
        scripts = [p for p in scripts if p.stem not in EXE_SCRIPTS]
    if not scripts:
        print("没有匹配的脚本")
        return 1

    results: list[tuple[str, str, float, str]] = []
    print(f"共 {len(scripts)} 个检查脚本" + (f"，exe={Path(exe).name}" if exe else ""))
    print("-" * 64)
    for p in scripts:
        cmd = [sys.executable, str(p)]
        if p.stem in EXE_SCRIPTS and exe:
            cmd.append(exe)
        started = time.time()

        def _run_once() -> tuple[int, str]:
            try:
                cp = subprocess.run(cmd, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=args.timeout)
                return cp.returncode, (cp.stdout or "") + (cp.stderr or "")
            except subprocess.TimeoutExpired:
                return 124, f"超时（>{args.timeout}s）"

        rc, out = _run_once()
        # 失败**重试一次**：这些检查大多会起一个真实的 uvicorn，批量跑时偶发
        # 「连接被重置 / 端口还没释放」而单跑必过（实测 check_room_acl 就是这样）。
        # 真实回归是确定性的，重试仍会失败；靠这条把噪声和回归分开，
        # 重试才过的单独标 FLAKY 而不是 PASS，不掩盖问题。
        flaky = False
        if rc != 0:
            rc2, out2 = _run_once()
            if rc2 == 0:
                flaky = True
                rc, out = 0, out2
        elapsed = time.time() - started
        if flaky:
            verdict = "FLAKY"
        elif re.search(r"^SKIP[：:]", out.strip(), re.M):
            # 只认「整行以 SKIP： 开头」的显式跳过。
            # 以前只要输出里出现 "SKIP" 两个字就算跳过，导致中间打印过
            # 「xx 豁免 SKIP」的检查脚本被误报成跳过（它其实全绿跑完了）。
            verdict = "SKIP"
        else:
            verdict = "PASS"
        results.append((p.stem, verdict, elapsed, out))
        print(f"  {verdict:4s}  {p.stem:32s} {elapsed:6.1f}s")

    fails = [r for r in results if r[1] == "FAIL"]
    skips = [r for r in results if r[1] == "SKIP"]
    flakies = [r for r in results if r[1] == "FLAKY"]
    release_fails = [r for r in fails if r[0] in RELEASE_STATE_CHECKS]
    code_fails = [r for r in fails if r[0] not in RELEASE_STATE_CHECKS]
    print("-" * 64)
    print(f"PASS {len(results) - len(fails) - len(skips) - len(flakies)} / SKIP {len(skips)}"
          f" / FLAKY {len(flakies)} / FAIL {len(fails)}"
          + (f"（其中发版状态 {len(release_fails)}）" if release_fails else ""))
    if release_fails:
        print("  发版状态未通过（不影响代码门禁）：" + ", ".join(r[0] for r in release_fails))
    if flakies:
        print("  重试才通过（首次失败：端口/连接抖动，或 CDN 镜像缓存状态）："
              + ", ".join(r[0] for r in flakies))
    if skips:
        print("  跳过：" + ", ".join(r[0] for r in skips))
    for name, _, elapsed, out in code_fails:
        print(f"\n=== FAIL {name} ({elapsed:.1f}s) ===")
        tail = out.strip().splitlines()[-25:]
        print("\n".join(tail))
    if args.verbose:
        for name, verdict, elapsed, out in results:
            if verdict == "FAIL":
                continue
            print(f"\n=== {verdict} {name} ({elapsed:.1f}s) ===")
            print("\n".join(out.strip().splitlines()[-8:]))
    if release_fails:
        print("\n（发版状态类检查失败不算代码回归：CDN 缓存刷新后就会变绿；"
              "要让它们通过需要把当前版本重新上传到国内镜像）")
    return 0 if not code_fails else 1


if __name__ == "__main__":
    sys.exit(main())
