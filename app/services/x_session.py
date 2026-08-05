"""X 完整 Cookie 的运行时文件保存与经 RSSHub 的连接验证。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import requests

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


class XTemporaryError(XSessionError):
    pass


@dataclass(slots=True)
class XCredentialStatus:
    """运行时文件的保存状态，不把它伪装成持久的连接状态。"""

    state: str
    message: str
    configured: bool


def remove_legacy_sqlite_x_credential(repository: Repository) -> None:
    """清除旧实现残留的 X 密文，避免同一 Cookie 在两处保存。"""

    repository.delete_connector_credential(X_CONNECTOR)


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

    return {"cookie_header": "; ".join(f"{name}={cookie_value}" for name, cookie_value in values.items())}


class XSessionService:
    """只管理 RSSHub 读取的完整 Cookie 文件，不保存 SQLite 副本。"""

    def __init__(
        self,
        settings: Settings,
        runtime_files: RssHubRuntimeFiles | None = None,
        validator: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_files = runtime_files or RssHubRuntimeFiles(settings)
        self._validator = validator

    def status(self) -> XCredentialStatus:
        """仅说明文件是否已保存；连接是否可用必须通过 RSSHub 实测。"""

        try:
            self._load_runtime_cookies()
        except XCredentialMissingError:
            return XCredentialStatus("missing", "尚未保存完整 X Cookie，X 账号暂不会抓取。", False)
        except XCredentialConfigurationError as exc:
            return XCredentialStatus("invalid", str(exc), False)
        return XCredentialStatus(
            "saved",
            "完整 X Cookie 已保存到 RSSHub 共享运行时文件；可随时点击“验证”让 RSSHub 实测。",
            True,
        )

    def save_from_web(self, cookie_value: str) -> XCredentialStatus:
        """写入候选文件并由 RSSHub 实测；失败时恢复此前有效的文件。"""

        candidate = parse_x_cookie(cookie_value)
        previous = self._load_runtime_cookies_or_none()
        self._write_runtime_file(candidate)
        try:
            self._validate_runtime_credential()
        except Exception as exc:
            self._restore_runtime_file(previous)
            raise self._safe_error(exc) from exc
        return self._verified_status()

    def test_saved(self) -> XCredentialStatus:
        """直接验证 RSSHub 当前可读取的 Cookie 文件。"""

        self._load_runtime_cookies()
        try:
            self._validate_runtime_credential()
        except Exception as exc:
            raise self._safe_error(exc) from exc
        return self._verified_status()

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

    def _load_runtime_cookies_or_none(self) -> dict[str, str] | None:
        try:
            return self._load_runtime_cookies()
        except XSessionError:
            return None

    def _load_runtime_cookies(self) -> dict[str, str]:
        try:
            payload = self.runtime_files.read_x_credential()
        except FileNotFoundError as exc:
            raise XCredentialMissingError("尚未保存完整 X Cookie，请在“设置与连接”页面保存后重试。") from exc
        except (OSError, UnicodeError, ValueError) as exc:
            raise XCredentialConfigurationError("已保存的 X Cookie 文件格式无效，请重新保存完整 Cookie。") from exc
        try:
            return parse_x_cookie(payload["cookie_header"])
        except (KeyError, XCredentialMissingError) as exc:
            raise XCredentialConfigurationError("已保存的 X Cookie 文件格式无效，请重新保存完整 Cookie。") from exc

    def _write_runtime_file(self, cookies: dict[str, str]) -> None:
        try:
            self.runtime_files.write_x_credential(cookies)
        except (OSError, ValueError) as exc:
            raise XCredentialConfigurationError("无法写入 RSSHub 的 X Cookie 共享文件，请检查数据目录权限。") from exc

    def _restore_runtime_file(self, previous: dict[str, str] | None) -> None:
        if previous:
            self._write_runtime_file(previous)
        else:
            self.runtime_files.clear_x_credential()

    @staticmethod
    def _verified_status() -> XCredentialStatus:
        return XCredentialStatus("verified", "RSSHub 已使用当前完整 Cookie 完成 X 抓取验证。", True)

    @staticmethod
    def _safe_error(exc: Exception) -> XSessionError:
        if isinstance(exc, XSessionError):
            return exc
        return XTemporaryError("RSSHub 暂时无法验证 X Cookie，请稍后重试。")
