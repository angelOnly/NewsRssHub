"""X 完整 Cookie 的加密保存与经 RSSHub 的连接验证。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable

import requests
from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings
from app.services.rsshub_runtime import RssHubRuntimeFiles
from app.storage.repository import Repository


X_CONNECTOR = "x_session"
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_REQUIRED_COOKIE_NAMES = ("auth_token", "ct0")


class XSessionError(RuntimeError):
    """面向用户的 X 连接错误；其中绝不能包含 Cookie 内容。"""


class XCredentialMissingError(XSessionError):
    pass


class XCredentialExpiredError(XSessionError):
    pass


class XCredentialConfigurationError(XSessionError):
    pass


class XCredentialFullCookieRequiredError(XSessionError):
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
    """规范化完整的 x.com Cookie，并要求会话所需的关键字段。"""

    raw = value.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if not raw:
        raise XCredentialMissingError("请粘贴 x.com 的完整 Cookie 字符串。")
    if "\r" in raw or "\n" in raw:
        raise XCredentialMissingError("Cookie 格式无效，请粘贴单行的 x.com Cookie 字符串。")

    values: dict[str, str] = {}
    for raw_part in raw.split(";"):
        part = raw_part.strip()
        if not part:
            continue
        name, separator, cookie_value = part.partition("=")
        name = name.strip()
        cookie_value = cookie_value.strip()
        if not separator or not _COOKIE_NAME.fullmatch(name) or not cookie_value:
            raise XCredentialMissingError("Cookie 格式无效，请粘贴 x.com 的完整 Cookie 字符串。")
        values[name] = cookie_value

    missing = [name for name in _REQUIRED_COOKIE_NAMES if not values.get(name)]
    if missing:
        labels = "、".join(missing)
        raise XCredentialMissingError(
            f"完整 X Cookie 中缺少 {labels}；请从 x.com 请求的 Cookie 头重新复制。"
        )

    return {
        "auth_token": values["auth_token"],
        "cookie_header": "; ".join(f"{name}={cookie_value}" for name, cookie_value in values.items()),
    }


def _fingerprint(cookies: dict[str, str]) -> str:
    return hashlib.sha256(cookies["cookie_header"].encode("utf-8")).hexdigest()[-10:]


class XSessionService:
    """保存完整 X Cookie，并同步给 RSSHub 的只读运行时文件。"""

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
            return XCredentialStatus("missing", "尚未保存完整 X Cookie，X 账号暂不会抓取。", False)
        state = str(record.get("status") or "unknown")
        last_error = str(record.get("last_error") or "")
        messages = {
            "valid": "完整 X Cookie 已验证，可添加多个 X 账号来源。",
            "needs_full_cookie": "旧版仅 auth_token 凭据已停用，请重新粘贴完整的 x.com Cookie。",
            "invalid": "X 登录 Cookie 已失效，请更新完整 Cookie 后再抓取。",
            "error": last_error or "暂时无法验证 X 登录状态，请稍后重试。",
        }
        return XCredentialStatus(
            state,
            messages.get(state, "尚未验证完整 X Cookie。"),
            state not in {"needs_full_cookie"},
            fingerprint=str(record.get("fingerprint") or ""),
            updated_at=record.get("updated_at"),
            last_validated_at=record.get("last_validated_at"),
            last_error=last_error,
        )

    def sync_runtime_file(self) -> None:
        """启动时恢复完整 Cookie；旧 token 凭据不会再进入 RSSHub。"""

        record = self.repository.get_connector_credential(X_CONNECTOR)
        if not record:
            self.runtime_files.clear_x_credential()
            return
        try:
            self.runtime_files.write_x_credential(self._load_cookies())
        except XCredentialFullCookieRequiredError as exc:
            self.runtime_files.clear_x_credential()
            self._mark_full_cookie_required(record, exc)
        except XSessionError:
            # 密钥配置或历史密文异常时宁可不给 RSSHub 残留凭据，也不能让服务无法启动。
            self.runtime_files.clear_x_credential()

    def save_from_web(self, cookie_value: str) -> XCredentialStatus:
        """验证候选完整 Cookie；失败时恢复之前已验证的完整会话。"""

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
        try:
            cookies = self._load_cookies()
        except XCredentialFullCookieRequiredError as exc:
            self.runtime_files.clear_x_credential()
            record = self.repository.get_connector_credential(X_CONNECTOR)
            if record:
                self._mark_full_cookie_required(record, exc)
            raise
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

    def record_rsshub_auth_failure(self, error: Exception) -> bool:
        """将 RSSHub 返回的明确 X 鉴权失败同步到设置页状态。"""

        response = getattr(error, "response", None)
        body = str(getattr(response, "text", "") or "")[:2000] if response is not None else ""
        details = f"{error}\n{body}".casefold()
        if "twitter api error: 401" not in details and "twitter api error: 403" not in details:
            return False
        self._record_failure(
            XCredentialExpiredError("X 登录 Cookie 已失效，请更新完整 Cookie 后重试。")
        )
        return True

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
            raise XCredentialExpiredError("X 登录 Cookie 已失效，请更新完整 Cookie 后重试。")
        if response.status_code == 404:
            raise XCredentialConfigurationError(
                "RSSHub 尚未部署 NewsRSSHub 自定义路由，请按部署说明更新 RSSHub 镜像。"
            )
        raise XTemporaryError("RSSHub 暂时无法验证 X Cookie，请稍后重试。")

    def _load_saved_cookies_or_none(self) -> dict[str, str] | None:
        if not self.repository.get_connector_credential(X_CONNECTOR):
            return None
        try:
            return self._load_cookies()
        except XCredentialFullCookieRequiredError:
            # 旧 token 已停用，候选完整 Cookie 验证失败后不能再恢复它。
            return None

    def _load_cookies(self) -> dict[str, str]:
        record = self.repository.get_connector_credential(X_CONNECTOR)
        if not record:
            raise XCredentialMissingError("尚未配置完整 X Cookie，请在“设置与连接”页面保存后重试。")
        try:
            decrypted = self._cipher().decrypt(str(record["ciphertext"]).encode("ascii"))
            payload = json.loads(decrypted.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
            raise XCredentialConfigurationError("已保存的 X Cookie 无法读取，请重新保存一次。") from exc
        if not isinstance(payload, dict):
            raise XCredentialConfigurationError("已保存的 X Cookie 格式无效，请重新保存完整 Cookie。")
        if payload.get("version") != 2 or not isinstance(payload.get("cookie_header"), str):
            raise XCredentialFullCookieRequiredError(
                "旧版仅 auth_token 凭据已停用，请重新粘贴完整的 x.com Cookie。"
            )
        try:
            return parse_x_cookie(str(payload["cookie_header"]))
        except XCredentialMissingError as exc:
            raise XCredentialConfigurationError(
                "已保存的 X Cookie 格式无效，请重新保存完整 Cookie。"
            ) from exc

    def _save_valid(self, cookies: dict[str, str]) -> None:
        sanitized = parse_x_cookie(cookies["cookie_header"])
        ciphertext = self._cipher().encrypt(
            json.dumps(
                {"version": 2, "cookie_header": sanitized["cookie_header"]},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        self.repository.save_connector_credential(
            connector=X_CONNECTOR,
            ciphertext=ciphertext,
            fingerprint=_fingerprint(sanitized),
            status="valid",
        )
        # SQLite 是可信的持久化存储；RSSHub 只读取完整 Cookie 的运行时副本。
        self.runtime_files.write_x_credential(sanitized)

    def _restore_runtime_file(self, previous: dict[str, str] | None) -> None:
        if previous:
            self.runtime_files.write_x_credential(previous)
        else:
            self.runtime_files.clear_x_credential()

    def _mark_full_cookie_required(self, record: dict[str, object], exc: XCredentialFullCookieRequiredError) -> None:
        if (
            str(record.get("status") or "") == "needs_full_cookie"
            and str(record.get("last_error") or "") == str(exc)
        ):
            return
        self.repository.update_connector_credential_health(
            X_CONNECTOR,
            status="needs_full_cookie",
            last_error=str(exc),
        )

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
