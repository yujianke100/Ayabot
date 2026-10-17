from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional

from bilibili_api import Credential, login_v2

from .config import AppConfig

# 凭证状态文件（写在房间 data 目录，WebUI 读取后引导用户重新登录）
AUTH_STATUS_FILE = "auth_status.json"
STATUS_OK = "ok"
STATUS_NEEDS_LOGIN = "needs_login"
STATUS_ERROR = "error"

# 等待扫码登录时的轮询间隔（秒）
_LOGIN_WAIT_POLL = 5.0
# 即使文件没变，也至少这么久重新校验一次凭证
_MAX_VALIDATE_AGE = 120.0


class CredentialError(RuntimeError):
    """凭证不可用且配置为不等待登录时抛出。"""


class AuthManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("bili-live-robot.auth")
        self._refresh_task: Optional[asyncio.Task[None]] = None
        self._status: str = STATUS_OK
        self._reason: str = ""
        # 上次校验时的凭证文件签名，避免每次轮询都打 B 站接口
        self._last_signature: Optional[tuple] = None
        self._last_validated: float = 0.0

    # ── 凭证状态（供 WebUI 展示/引导重新登录） ────────────────

    def status_path(self) -> Path:
        """房间 data 目录下的 auth_status.json。"""
        try:
            base = Path(self.config.storage.sqlite_path).parent
        except Exception:  # pragma: no cover - 配置异常兜底
            base = Path("data")
        return base / AUTH_STATUS_FILE

    def write_status(self, status: str, reason: str = "") -> None:
        self._status = status
        self._reason = reason
        payload = {
            "status": status,
            "reason": reason,
            "account_uid": self.config.account_uid,
            "room_id": str(self.config.room_display_id),
            "ts": int(time.time()),
            "pid": __import__("os").getpid(),
        }
        try:
            path = self.status_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("write auth status failed: %s", exc)

    def mark_needs_login(self, reason: str) -> None:
        if self._status != STATUS_NEEDS_LOGIN or self._reason != reason:
            self.logger.warning("credential unusable: %s (等待重新登录)", reason)
        self.write_status(STATUS_NEEDS_LOGIN, reason)

    def mark_ok(self) -> None:
        if self._status != STATUS_OK:
            self.logger.info("credential restored")
        self.write_status(STATUS_OK, "")

    def load_status(self) -> dict:
        try:
            path = self.status_path()
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:  # noqa: BLE001
            pass
        return {"status": STATUS_OK, "reason": ""}

    # ── 凭证加载 ────────────────────────────────────────────

    async def prepare_credential(self) -> Credential:
        """加载凭证：有效则直接返回；失效则保持进程运行等待用户重新扫码。

        旧行为是直接抛 RuntimeError，导致房间进程启动即退出、WebUI 只显示
        「启动失败」，用户不知道要重新登录。现在改为写入 auth_status.json
        并在后台轮询 accounts/<uid>/credential.json，扫码成功后自动恢复。
        """
        credential = self.load_credential_sync()
        if credential is not None and await self._is_valid(credential):
            self.logger.info("loaded valid credential")
            self.mark_ok()
            self._save_credential(credential)
            self.mark_validated()
            return credential

        reason = (
            "B站登录凭证已失效（Cookie 过期或被踢下线）"
            if credential is not None
            else "未找到 B站 登录凭证"
        )
        self.mark_needs_login(reason)

        if not self.config.auth.wait_for_login:
            raise CredentialError(
                f"{reason}。请到 WebUI「用户管理 → B站账号」扫码登录后重启房间。"
            )

        self.logger.warning(
            "%s — 房间保持运行，等待扫码登录（WebUI → 用户管理 → B站账号）", reason
        )
        return await self.wait_for_relogin()

    async def wait_for_relogin(self) -> Credential:
        """阻塞轮询，直到磁盘上出现有效凭证。"""
        last_reason = self._reason or "登录凭证不可用"
        while True:
            await asyncio.sleep(_LOGIN_WAIT_POLL)
            credential = self.load_credential_sync()
            if credential is None:
                continue
            if not await self._is_valid(credential):
                self.mark_needs_login(last_reason)
                continue
            self.logger.info("扫码登录成功，凭证已恢复，机器人继续运行")
            self.mark_ok()
            self._save_credential(credential)
            self.mark_validated()
            return credential

    def load_credential_sync(self) -> Optional[Credential]:
        """从账号目录/房间目录读取凭证（不做有效性校验）。"""
        credential = self._load_stored_credential()
        if credential is not None:
            return credential
        return self._credential_from_config()

    # ── 凭证热更新（运行中失效后重新登录的场景） ─────────────

    def _signature(self) -> tuple:
        """凭证文件的 (路径, mtime, size) 签名，用于判断磁盘上是否有新凭证。"""
        sig: list[tuple] = []
        for path in self._credential_candidate_paths():
            try:
                st = path.stat()
                sig.append((str(path), int(st.st_mtime), st.st_size))
            except OSError:
                continue
        return tuple(sig)

    def _credential_candidate_paths(self) -> list[Path]:
        paths: list[Path] = []
        uid = self.config.account_uid
        if uid:
            store_path = Path(self.config.auth.credential_store_path)
            project_root = (
                store_path.parent.parent.parent.parent if store_path.is_absolute() else Path()
            )
            paths.append(project_root / "accounts" / uid / "credential.json")
        paths.append(Path(self.config.auth.credential_store_path))
        return paths

    async def try_pick_up_new_credential(self) -> Optional[Credential]:
        """磁盘凭证有变化且有效时返回新凭证，否则 None。

        用于「运行中凭证失效 → 用户重新扫码 → 机器人自动恢复」的场景。
        """
        sig = self._signature()
        if sig == self._last_signature and time.time() - self._last_validated < _MAX_VALIDATE_AGE:
            return None
        credential = self.load_credential_sync()
        if credential is None:
            return None
        if not await self._is_valid(credential):
            return None
        self._last_signature = sig
        self._last_validated = time.time()
        return credential

    def mark_validated(self) -> None:
        self._last_signature = self._signature()
        self._last_validated = time.time()

    # ── 后台刷新 ────────────────────────────────────────────

    def start_refresh_loop(self, credential: Credential) -> None:
        if self._refresh_task is not None:
            return
        self._refresh_task = asyncio.create_task(self._refresh_loop(credential))

    async def stop(self) -> None:
        if self._refresh_task is None:
            return
        self._refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._refresh_task
        self._refresh_task = None

    def _credential_from_config(self) -> Optional[Credential]:
        c = self.config.credential
        if not c.sessdata or not c.bili_jct:
            return None
        return Credential(
            sessdata=c.sessdata,
            bili_jct=c.bili_jct,
            buvid3=c.buvid3 or None,
            dedeuserid=c.dedeuserid or None,
        )

    def _load_stored_credential(self) -> Optional[Credential]:
        # 优先从统一的 accounts/<uid>/credential.json 加载
        uid = self.config.account_uid
        if uid:
            # 从 credential_store_path 反推项目根目录
            # credential_store_path = /root/ayabot/rooms/<id>/data/credential.json
            # 项目根 = 上4级 = /root/ayabot/
            store_path = Path(self.config.auth.credential_store_path)
            project_root = store_path.parent.parent.parent.parent if store_path.is_absolute() else Path()
            accounts_path = project_root / "accounts" / uid / "credential.json"
            if accounts_path.exists():
                return self._load_credential_file(accounts_path)

        # 降级到旧的 per-room 路径
        path = Path(self.config.auth.credential_store_path)
        if path.exists():
            return self._load_credential_file(path)
        return None

    def _load_credential_file(self, path: Path) -> Optional[Credential]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            cookies = {
                "SESSDATA": raw.get("SESSDATA", ""),
                "bili_jct": raw.get("bili_jct", ""),
                "buvid3": raw.get("buvid3", ""),
                "DedeUserID": raw.get("DedeUserID", ""),
                "ac_time_value": raw.get("ac_time_value", ""),
            }
            cred = Credential.from_cookies(cookies)
            self.logger.debug("credential loaded from %s", path)
            return cred
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("failed to load credential store: %s", exc)
            return None

    def _save_credential(self, credential: Credential) -> None:
        path = Path(self.config.auth.credential_store_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        cookies = credential.get_cookies()
        payload = {
            "SESSDATA": cookies.get("SESSDATA", ""),
            "bili_jct": cookies.get("bili_jct", ""),
            "buvid3": cookies.get("buvid3", ""),
            "DedeUserID": cookies.get("DedeUserID", ""),
            "ac_time_value": cookies.get("ac_time_value", ""),
        }

        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _is_valid(self, credential: Credential) -> bool:
        try:
            valid = bool(await credential.check_valid())
        except Exception as exc:  # noqa: BLE001
            # 网络抖动时不要误判为「凭证有效」，交给上层 5s 后重试
            self.logger.warning("credential validity check failed: %s", exc)
            return False
        self._last_validated = time.time()
        return valid

    async def _qr_login(self) -> Credential:
        """WebUI 账号管理已接管 QR 登录，此方法保留仅作兜底。"""
        qr_login = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)
        await qr_login.generate_qrcode()

        self.logger.info("qr login started — scan via WebUI → B站账号")
        url = qr_login.get_qrcode_url()
        self.logger.info("qrcode url: %s", url)

        last_state = None
        while not qr_login.has_done():
            state = await qr_login.check_state()
            if state != last_state:
                self.logger.info("qr login state: %s", state.value)
                last_state = state

            if state == login_v2.QrCodeLoginEvents.TIMEOUT:
                raise RuntimeError("QR code timeout, please restart and scan again")

            await asyncio.sleep(max(self.config.auth.qr_poll_seconds, 0.5))

        credential = qr_login.get_credential()
        self.logger.info("qr login success")
        return credential

    async def _refresh_loop(self, credential: Credential) -> None:
        interval = max(self.config.auth.refresh_interval_seconds, 300)
        while True:
            await asyncio.sleep(interval)

            try:
                valid = await credential.check_valid()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("periodic valid check failed: %s", exc)
                continue

            if not valid:
                self.mark_needs_login("B站登录凭证已失效（Cookie 过期或被踢下线）")
                continue

            # 凭证恢复（用户已重新扫码）→ 清掉告警
            if self._status != STATUS_OK:
                self.mark_ok()

            if not credential.has_ac_time_value():
                self.logger.debug("skip refresh check: no ac_time_value")
                continue

            try:
                need_refresh = await credential.check_refresh()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("refresh check failed: %s", exc)
                continue

            if not need_refresh:
                continue

            try:
                await credential.refresh()
                self._save_credential(credential)
                self.logger.info("credential refreshed and persisted")
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("credential refresh failed: %s", exc)
