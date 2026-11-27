"""「网络不通」不等于「凭证失效」—— 一个把用户骗去重扫二维码的坑。

`Credential.check_valid()` 在**问不到** B站 时会抛异常（DNS 污染、断网、
接口 5xx、限流）。原来的代码把「抛异常」和「isLogin=false」当成同一件事：

    async def _is_valid(self, credential) -> bool:
        try:
            valid = bool(await credential.check_valid())
        except Exception:
            return False        # ← 网络问题被判定成「凭证过期」

后果，两条都直接对应用户反馈：

1. 启动时网络抖一下，机器人立刻写 `auth_status.json = needs_login`，
   界面红条：「B站 登录凭证已失效（Cookie 过期或被踢下线）」+「去扫码登录」。
   cookie 其实完好无损，用户白扫一遍。
2. 账号管理页「一键检测所有」在断网时把每个账号都标成「❌ 失效」。

这里不起真服务器、不碰网络，直接构造 AuthManager 并替换 `check_valid` 的行为，
验证三种情形分别得到什么结论。

    ./.venv/Scripts/python.exe scripts/check_credential_network.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def build_cfg(tmp: Path):
    import yaml
    src = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    src["room_display_id"] = 1946287911
    src["anchor_uid"] = 8888
    src["account_uid"] = "123456"
    src["storage"] = {"sqlite_path": str(tmp / "data" / "bot.db")}
    src["auth"] = dict(src.get("auth") or {},
                       credential_store_path=str(tmp / "data" / "credential.json"),
                       wait_for_login=True)
    src["runtime"] = dict(src.get("runtime") or {}, timezone="Asia/Shanghai", log_level="CRITICAL")
    f = src.setdefault("features", {})
    f["thanks_enabled"] = False
    f["welcome_enabled"] = False
    cfg = tmp / "config.yaml"
    cfg.write_text(yaml.safe_dump(src, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return cfg


def main() -> int:
    from app.auth import AuthManager
    from app.config import load_config

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    tmp = Path(tempfile.mkdtemp(prefix="crednet_"))
    try:
        (tmp / "data").mkdir(parents=True, exist_ok=True)
        cred_path = tmp / "data" / "credential.json"
        cred_path.write_text(json.dumps({
            "SESSDATA": "s", "bili_jct": "j", "buvid3": "b",
            "DedeUserID": "123456", "ac_time_value": "a",
        }), encoding="utf-8")
        cfg = load_config(str(build_cfg(tmp)))

        print("[1] 网络不通（check_valid 抛异常）→ 三态必须是 None，不能是 False")
        am = AuthManager(cfg)
        cred = am.load_credential_sync()
        ok("凭证能从磁盘读到", cred is not None)

        async def boom():
            raise RuntimeError("Cannot connect to host api.bilibili.com:443")

        cred.check_valid = boom  # type: ignore[assignment]
        got = asyncio.run(am._is_valid(cred))
        ok("_is_valid 返回 None（没问成）", got is None, repr(got))

        async def says_invalid():
            return False
        cred.check_valid = says_invalid  # type: ignore[assignment]
        got2 = asyncio.run(am._is_valid(cred))
        ok("B站 明确说无效 → False", got2 is False, repr(got2))

        async def says_valid():
            return True
        cred.check_valid = says_valid  # type: ignore[assignment]
        got3 = asyncio.run(am._is_valid(cred))
        ok("B站 明确说有效 → True", got3 is True, repr(got3))

        print("[2] 启动时网络不通：不能写 needs_login，必须照常启动")
        am2 = AuthManager(cfg)
        cred2 = am2.load_credential_sync()
        cred2.check_valid = boom  # type: ignore[assignment]
        am2.load_credential_sync = lambda: cred2  # type: ignore[assignment]
        # 把重试间隔压到 0，测试才快
        import app.auth as auth_mod
        old_delay = auth_mod._VALIDATE_RETRY_DELAY
        auth_mod._VALIDATE_RETRY_DELAY = 0.0
        returned = None
        errored = ""
        try:
            returned = asyncio.run(asyncio.wait_for(am2.prepare_credential(), timeout=10))
        except Exception as exc:  # noqa: BLE001
            # 旧行为会在这里卡进 wait_for_relogin（等扫码）然后被 timeout 打断。
            # 必须把它当成失败报出来，而不是让整个脚本崩掉 —— 否则反向验证
            # 只能看到第一条断言变红，后面全是"没跑到"。
            errored = f"{type(exc).__name__}: {exc}"
        finally:
            auth_mod._VALIDATE_RETRY_DELAY = old_delay
        ok("prepare_credential 没有卡住等待扫码", returned is not None, errored)
        status = am2.load_status()
        ok("**没有**被写成 needs_login", status.get("status") != "needs_login",
           status.get("status"))
        ok("状态是 ok（先用着）", status.get("status") == "ok", status.get("status"))

        print("[3] 启动时 B站 明确说失效：仍然要走「去扫码」流程")
        am3 = AuthManager(cfg)
        cred3 = am3.load_credential_sync()
        cred3.check_valid = says_invalid  # type: ignore[assignment]
        am3.load_credential_sync = lambda: cred3  # type: ignore[assignment]
        # 让它别真的阻塞等待：wait_for_login 关掉就会抛 CredentialError
        object.__setattr__(am3.config.auth, "wait_for_login", False) \
            if hasattr(am3.config.auth, "__dataclass_fields__") else None
        am3.config.auth.wait_for_login = False
        try:
            asyncio.run(asyncio.wait_for(am3.prepare_credential(), timeout=10))
            raised = False
        except Exception as exc:  # noqa: BLE001
            raised = type(exc).__name__
        from app.auth import CredentialError
        ok("抛 CredentialError（引导用户重新登录）", bool(raised), raised)
        st3 = am3.load_status()
        ok("写入了 needs_login", st3.get("status") == "needs_login", st3.get("status"))

        print("[4] 运行中热更新：网络不通不能把旧凭证当新的")
        am4 = AuthManager(cfg)
        cred4 = am4.load_credential_sync()
        cred4.check_valid = boom  # type: ignore[assignment]
        am4.load_credential_sync = lambda: cred4  # type: ignore[assignment]
        am4._last_signature = ""     # 强制重新检查
        picked = asyncio.run(am4.try_pick_up_new_credential())
        ok("问不到时不返回凭证（保持原状）", picked is None, picked)

        print("[5] 校验接口的三态返回（结构断言）")
        src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        seg = src[src.index("async def api_verify_account_credential"):]
        seg = seg[: seg.index("\n@app")]
        ok("异常分支返回 valid=None 而不是 False", '"valid": None' in seg)
        ok("异常分支标了 unknown", '"unknown": True' in seg)
        ok("失败时带上原因（前端可以显示）", '"error"' in seg)
        fseg = src[src.index("async function _processVerifyQueue"):]
        fseg = fseg[: fseg.index("\n        }")]
        ok("前端区分 unknown（不再一律当失效）", "credential_unknown" in fseg)
        ok("请求本身失败也归为 unknown", "acct.credential_unknown = true" in fseg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("CREDENTIAL NETWORK OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
