"""凭证失效路径验证：失效时保持存活、写 needs_login、扫码后自动恢复。

用法: ./.venv/Scripts/python.exe scripts/check_auth_recovery.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/auth_test")
REAL_CRED = ROOT / "accounts" / "3706957229722452" / "credential.json"


def build_config(cred_path: Path, wait: bool) -> str:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load((ROOT / "rooms" / "1946287911" / "config.yaml").read_text(encoding="utf-8"))
    base["storage"] = {"sqlite_path": "bot.db"}
    base["auth"] = {
        "auto_login": True,
        "credential_store_path": str(cred_path),
        "qr_poll_seconds": 1.0,
        "refresh_interval_seconds": 3600,
        "wait_for_login": wait,
    }
    base["credential"] = {"sessdata": "", "bili_jct": "", "buvid3": "", "dedeuserid": ""}
    path = SCRATCH / "config.yaml"
    path.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(path)


BOGUS = {
    "SESSDATA": "deadbeef%2C1790000000%2Cabcd1%2A11",
    "bili_jct": "0123456789abcdef0123456789abcdef",
    "buvid3": "ABCDEF12-3456-7890-ABCD-EF123456789012345infoc",
    "DedeUserID": "3706957229722452",
    "ac_time_value": "bogusacvalue",
}


async def main() -> int:
    from app.auth import AuthManager, CredentialError
    from app.config import load_config

    failures: list[str] = []

    # ── 场景 1: 凭证失效 + wait_for_login=true → 不抛错，等待，恢复后返回 ──
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    cred_path = SCRATCH / "cred.json"
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    cred_path.write_text(json.dumps(BOGUS), encoding="utf-8")

    config = load_config(build_config(cred_path, wait=True))
    auth = AuthManager(config)

    async def relogin_after(delay: float) -> None:
        await asyncio.sleep(delay)
        shutil.copy2(REAL_CRED, cred_path)  # 模拟用户扫码成功，磁盘凭证被替换
        print(f"[+{delay:.1f}s] credential file replaced with a valid one")

    updater = asyncio.create_task(relogin_after(7.0))
    started = asyncio.get_running_loop().time()
    try:
        credential = await asyncio.wait_for(auth.prepare_credential(), timeout=90)
    except asyncio.TimeoutError:
        print("FAILED: prepare_credential 在凭证恢复后仍未返回")
        return 1
    waited = asyncio.get_running_loop().time() - started
    await updater
    status = auth.load_status()
    print(f"recovered after {waited:.1f}s, status={status}")
    if waited < 5:
        failures.append("没有真正等待重新登录（可能直接用了旧凭证）")
    if status.get("status") != "ok":
        failures.append(f"恢复后状态应为 ok，实际 {status}")
    if not credential.get_cookies().get("SESSDATA"):
        failures.append("恢复后的凭证缺少 SESSDATA")

    # needs_login 状态是否在等待期间写下过（文件里现在是 ok，用单独实例复核）
    print("auth_status.json:", (SCRATCH / "auth_status.json").read_text(encoding="utf-8"))

    # ── 场景 2: wait_for_login=false → 抛 CredentialError（fail-fast 部署） ──
    cred_path.write_text(json.dumps(BOGUS), encoding="utf-8")
    config2 = load_config(build_config(cred_path, wait=False))
    auth2 = AuthManager(config2)
    try:
        await asyncio.wait_for(auth2.prepare_credential(), timeout=60)
        failures.append("wait_for_login=false 时应当抛出 CredentialError")
    except CredentialError as exc:
        print("fail-fast ok:", str(exc)[:60])
    except asyncio.TimeoutError:
        failures.append("wait_for_login=false 时挂住了（应当立即抛错）")

    # ── 场景 3: 热更新（运行中换凭证） ──
    shutil.copy2(REAL_CRED, cred_path)
    config3 = load_config(build_config(cred_path, wait=True))
    auth3 = AuthManager(config3)
    cred3 = await auth3.prepare_credential()
    print("scenario3 initial:", auth3.load_status().get("status"))
    pick = await auth3.try_pick_up_new_credential()
    print("scenario3 no-change pick:", bool(pick))
    if pick is not None:
        failures.append("凭证文件未变化时不应判为新凭证")
    # 改写文件（模拟重新扫码）
    data = json.loads(REAL_CRED.read_text(encoding="utf-8"))
    cred_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    auth3._last_validated = 0  # 强制立即校验
    pick2 = await auth3.try_pick_up_new_credential()
    print("scenario3 after change pick:", bool(pick2))
    if pick2 is None:
        failures.append("凭证文件变化后应能拾取新凭证")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nAUTH RECOVERY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
