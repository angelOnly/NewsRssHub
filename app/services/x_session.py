"""X Cookie 的加密保存与经 RSSHub 的连接验证。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

import requests
from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings
from app.services.rsshub_runtime import RssHubRuntimeFiles
from app.storage.repository import Repository


X_CONNECTOR = "x_session"


class XSessionError(RuntimeError):
    """面向用户的 X 连接错误；其中绝不能包含 Cookie 内容。"""


class XCredentialMissingError(XSessionError):
    pass


class XCredentialExpiredError(XSessionError):
    pass


class XCredentialConfigurationError(XSessionError):
    pass


class XTemporaryError(XSessionError):
    pass


@dataclass(slots=True)
class XCredentialStatus:
    state: str
    message: str
    configured: bool
    fingerprint: str = ""
    updated_at: str | None = None
    last_validated_at: str | None = None
    last_error: str = ""


def parse_x_cookie(value: str) -> dict[str, str]:
    """接受 auth_token 值或浏览器 Cookie 片段，但只保留 auth_token。"""

    raw = value.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if not raw:
        raise XCredentialMissingError("请粘贴 X 的 auth_token Cookie。")

    if "auth_token=" not in raw:
        auth_token = raw
        if any(character.isspace() for character in auth_token):
            raise XCredentialMissingError("请输入 auth_token 的值，或完整的 Cookie 字符串。")
    else:
        auth_token = ""
        for part in raw.split(";"):
            name, separator, cookie_value = part.strip().partition("=")
            if separator and name.strip() == "auth_token" and cookie_value.strip():
                auth_token = cookie_value.strip()
                break

    if not auth_token:
        raise XCredentialMissingError("未找到 auth_token；请从 x.com 的 Cookie 中复制该值。")
    return {"auth_token": auth_token}


def _fingerprint(cookies: dict[str, str]) -> str:
    return hashlib.sha256(cookies["auth_token"].encode("utf-8")).hexdigest()[-10:]


class XSessionService:
    """将 X Cookie 保存在 SQLite，并同步给 RSSHub 的只读运行时文件。"""

    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        runtime_files: RssHubRuntimeFiles | None = None,
        validator: Callable[[], None] | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.runtime_files = runtime_files or RssHubRuntimeFiles(settings)
        self._validator = validator

    def _cipher(self) -> Fernet:
        raw_key = self.settings.credential_encryption_key
        if not raw_key:
            raise XCredentialConfigurationError(
                "尚未配置 CREDENTIAL_ENCRYPTION_KEY，无法安全保存 X Cookie。"
            )
        try:
            return Fernet(raw_key.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise XCredentialConfigurationError(
                "CREDENTIAL_ENCRYPTION_KEY 格式无效，无法安全保存 X Cookie。"
            ) from exc

    def status(self) -> XCredentialStatus:
        try:
            self._cipher()
        except XCredentialConfigurationError as exc:
            return XCredentialStatus("needs_key", str(exc), False)
        record = self.repository.get_connector_credential(X_CONNECTOR)
        if not record:
            return XCredentialStatus("missing", "尚未保存 X 登录 Cookie，X 账号暂不会抓取。", False)
        state = str(record.get("status") or "unknown")
        last_error = str(record.get("last_error") or "")
        messages = {
            "valid": "X 登录 Cookie 可用。",
            "invalid": "X 登录 Cookie 已失效，请在此更新后再抓取。",
            "error": last_error or "暂时无法验证 X 登录状态，请稍后重试。",
        }
        return XCredentialStatus(
            state,
            messages.get(state, "尚未验证 X 登录 Cookie。"),
            True,
            fingerprint=str(record.get("fingerprint") or ""),
            updated_at=record.get("updated_at"),
            last_validated_at=record.get("last_validated_at"),
            last_error=last_error,
        )

    def sync_runtime_file(self) -> None:
        """启动时从已加密的 SQLite 恢复共享文件，兼容升级前的已存凭据。"""

        if not self.repository.get_connector_credential(X_CONNECTOR):
            self.runtime_files.clear_x_credential()
            return
        try:
            self.runtime_files.write_x_credential(self._load_cookies())
        except XSessionError:
            # 密钥配置或历史密文异常时宁可不给 RSSHub 残留凭据，也不能让服务无法启动。
            self.runtime_files.clear_x_credential()

    def save_from_web(self, cookie_value: str) -> XCredentialStatus:
        """验证候选 Cookie；失败时恢复旧共享文件，绝不覆盖已验证的 SQLite 记录。"""

        candidate = parse_x_cookie(cookie_value)
        self._cipher()
        previous = self._load_saved_cookies_or_none()
        self.runtime_files.write_x_credential(candidate)
        try:
            self._validate_runtime_credential()
        except Exception as exc:
            self._restore_runtime_file(previous)
            raise self._safe_error(exc) from exc
        self._save_valid(candidate)
        return self.status()

    def test_saved(self) -> XCredentialStatus:
        cookies = self._load_cookies()
        # RSSHub 容器可能在应用重启前后才创建；每次测试前都重新同步一次。
        self.runtime_files.write_x_credential(cookies)
        try:
            self._validate_runtime_credential()
        except Exception as exc:
            safe_error = self._safe_error(exc)
            self._record_failure(safe_error)
            raise safe_error from exc
        self._save_valid(cookies)
        return self.status()

    def _validate_runtime_credential(self) -> None:
        if self._validator:
            self._validator()
            return
        if not self.settings.rsshub_base_url:
            raise XCredentialConfigurationError(
                "请先在 config.yml 的 app.rsshub_base_url 配置已部署的 RSSHub 地址。"
            )

        try:
            response = requests.get(
                f"{self.settings.rsshub_base_url}/newsrsshub/x/validate",
                timeout=self.settings.request_timeout,
                headers={"Accept": "application/json"},
            )
        except requests.Timeout as exc:
            raise XTemporaryError("RSSHub 验证 X Cookie 超时，请稍后重试。") from exc
        except requests.RequestException as exc:
            raise XTemporaryError("暂时无法连接 RSSHub 验证 X Cookie，请稍后重试。") from exc

        if response.ok:
            return
        body = response.text[:200]
        if response.status_code in {401, 403} or "Twitter API error: 401" in body or "Twitter API error: 403" in body:
            raise XCredentialExpiredError("X 登录 Cookie 已失效，请更新后重试。")
        if response.status_code == 404:
            raise XCredentialConfigurationError(
                "RSSHub 尚未部署 NewsRSSHub 自定义路由，请按部署说明更新 RSSHub 镜像。"
            )
        raise XTemporaryError("RSSHub 暂时无法验证 X Cookie，请稍后重试。")

    def _load_saved_cookies_or_none(self) -> dict[str, str] | None:
        if not self.repository.get_connector_credential(X_CONNECTOR):
            return None
        return self._load_cookies()

    def _load_cookies(self) -> dict[str, str]:
        record = self.repository.get_connector_credential(X_CONNECTOR)
        if not record:
            raise XCredentialMissingError("X 登录 Cookie 未配置，请在“设置与连接”页面保存后重试。")
        try:
            decrypted = self._cipher().decrypt(str(record["ciphertext"]).encode("ascii"))
            payload = json.loads(decrypted.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
            raise XCredentialConfigurationError("已保存的 X Cookie 无法读取，请重新保存一次。") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("auth_token"), str):
            raise XCredentialConfigurationError("已保存的 X Cookie 格式无效，请重新保存一次。")
        auth_token = str(payload["auth_token"]).strip()
        if not auth_token:
            raise XCredentialConfigurationError("已保存的 X Cookie 格式无效，请重新保存一次。")
        return {"auth_token": auth_token}

    def _save_valid(self, cookies: dict[str, str]) -> None:
        sanitized = {"auth_token": str(cookies["auth_token"]).strip()}
        ciphertext = self._cipher().encrypt(
            json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        self.repository.save_connector_credential(
            connector=X_CONNECTOR,
            ciphertext=ciphertext,
            fingerprint=_fingerprint(sanitized),
            status="valid",
        )
        # SQLite 是可信的持久化存储；RSSHub 只读取这个仅含 auth_token 的运行时副本。
        self.runtime_files.write_x_credential(sanitized)

    def _restore_runtime_file(self, previous: dict[str, str] | None) -> None:
        if previous:
            self.runtime_files.write_x_credential(previous)
        else:
            self.runtime_files.clear_x_credential()

    def _record_failure(self, exc: XSessionError) -> None:
        if not self.repository.get_connector_credential(X_CONNECTOR):
            return
        status = "invalid" if isinstance(exc, XCredentialExpiredError) else "error"
        self.repository.update_connector_credential_health(
            X_CONNECTOR,
            status=status,
            last_error=str(exc),
        )

    @staticmethod
    def _safe_error(exc: Exception) -> XSessionError:
        if isinstance(exc, XSessionError):
            return exc
        return XTemporaryError("RSSHub 暂时无法验证 X Cookie，请稍后重试。")
