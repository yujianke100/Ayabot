from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import Optional

from bilibili_api import Credential, login_v2

from .config import AppConfig


class AuthManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("bili-live-robot.auth")
        self._refresh_task: Optional[asyncio.Task[None]] = None

    async def prepare_credential(self) -> Credential:
        credential = self._load_stored_credential()
        if credential is None:
            credential = self._credential_from_config()

        if credential is not None and await self._is_valid(credential):
            self.logger.info("loaded valid credential")
            self._save_credential(credential)
            return credential

        if not self.config.auth.auto_login:
            raise RuntimeError("No valid credential and auto_login is disabled")

        self.logger.info("no valid credential found")
        # 终端不再打印二维码，用户需通过 WebUI "B站账号"功能扫码登录
        raise RuntimeError(
            "No valid B站 credential. "
            "Please login via WebUI → B站账号 → 扫码登录, "
            "then restart the room."
        )

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
            self.logger.info("account_uid=%s but no credential at %s, fallback", uid, accounts_path)

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
            self.logger.info("credential loaded from %s", path)
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
            return bool(await credential.check_valid())
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("credential validity check failed: %s", exc)
            return False

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
                self.logger.warning("credential invalid, waiting manual relogin")
                continue

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
