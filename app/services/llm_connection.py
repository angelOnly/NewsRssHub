"""从 config.yml 读取并验证 OpenAI 兼容模型连接。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from app.config import Settings
from app.storage.repository import Repository


class LLMConnectionError(RuntimeError):
    """面向用户的安全错误，绝不包含 API Key 或响应正文。"""


class LLMCredentialMissingError(LLMConnectionError):
    pass


class LLMConfigurationError(LLMConnectionError):
    pass


class LLMAuthenticationError(LLMConnectionError):
    pass


class LLMTemporaryError(LLMConnectionError):
    pass


class LLMResponseError(LLMConnectionError):
    pass


@dataclass(frozen=True, slots=True)
class LLMRuntimeConfig:
    """Worker 实际调用模型所需的最小运行配置。"""

    api_key: str
    base_url: str
    model_name: str
    enabled: bool
    source: str
    request_timeout: int


@dataclass(slots=True)
class LLMConnectionStatus:
    state: str
    message: str
    configured: bool
    enabled: bool
    source: str
    base_url: str
    model_name: str
    fingerprint: str = ""
    updated_at: str | None = None
    last_validated_at: str | None = None
    last_error: str = ""


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[-10:]


def _normalize_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise LLMConfigurationError("请输入完整的模型 Base URL，例如 https://api.openai.com/v1。")
    if parsed.username or parsed.password or any(char.isspace() for char in base_url):
        raise LLMConfigurationError("模型 Base URL 格式无效。")
    return base_url


def _normalize_model_name(value: str) -> str:
    model_name = value.strip()
    if not model_name:
        raise LLMConfigurationError("请填写模型名称。")
    if len(model_name) > 160 or any(char in model_name for char in "\r\n\t"):
        raise LLMConfigurationError("模型名称格式无效。")
    return model_name


class LLMConnectionService:
    """统一使用 config.yml，网页只负责展示与验证，不再读写 SQLite 模型配置。"""

    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        request_post: Callable[..., Any] | None = None,
    ) -> None:
        # 保留 repository 参数以兼容现有服务装配；模型连接本身不再访问数据库。
        self.repository = repository
        self.settings = settings
        self._request_post = request_post or requests.post

    def status(self) -> LLMConnectionStatus:
        try:
            config = self._file_config()
        except LLMConnectionError as exc:
            return LLMConnectionStatus(
                state="error",
                message=str(exc),
                configured=False,
                enabled=False,
                source="config",
                base_url="",
                model_name="",
            )
        if config:
            return LLMConnectionStatus(
                state="config",
                message="正在使用 config.yml 中的模型配置；网页不会读取或保存 SQLite 模型配置。",
                configured=True,
                enabled=config.enabled,
                source="config",
                base_url=config.base_url,
                model_name=config.model_name,
                fingerprint=_fingerprint(config.api_key),
            )
        return LLMConnectionStatus(
            state="missing",
            message="尚未在 config.yml 配置模型 API Key；长内容会保持待摘要，资讯不会进入语义筛选。",
            configured=False,
            enabled=self.settings.llm_enabled,
            source="config",
            base_url=self.settings.openai_base_url,
            model_name=self.settings.openai_model_name,
        )

    def runtime_config(self) -> LLMRuntimeConfig | None:
        """所有 Worker 只读取 config.yml，绝不让 SQLite 覆盖模型连接。"""

        try:
            return self._file_config()
        except LLMConnectionError:
            return None

    def save_from_web(
        self,
        *,
        api_key_value: str,
        base_url: str,
        model_name: str,
        enabled: bool,
    ) -> LLMConnectionStatus:
        """保留旧路由的明确错误，避免旧页面悄悄写入无效 SQLite 配置。"""

        del api_key_value, base_url, model_name, enabled
        raise LLMConfigurationError("模型连接只从 config.yml 读取，请修改文件后重新部署或重启对应服务。")

    def test_saved(self) -> LLMConnectionStatus:
        """不修改配置，仅验证 config.yml 当前生效的模型。"""

        config = self.runtime_config()
        if not config:
            raise LLMCredentialMissingError("尚未在 config.yml 配置模型 API Key。")
        self._test_config(config)
        return self.status()

    def _file_config(self) -> LLMRuntimeConfig | None:
        if not self.settings.openai_api_key:
            return None
        return LLMRuntimeConfig(
            api_key=self.settings.openai_api_key,
            base_url=_normalize_base_url(self.settings.openai_base_url),
            model_name=_normalize_model_name(self.settings.openai_model_name),
            enabled=self.settings.llm_enabled,
            source="config",
            request_timeout=self.settings.request_timeout,
        )

    def _test_config(self, config: LLMRuntimeConfig) -> None:
        # 连接测试同样不覆盖模型默认采样参数，并关闭当前网关支持的思考模式。
        body = {
            "model": config.model_name,
            "messages": [
                {"role": "system", "content": "Reply with a short acknowledgement."},
                {"role": "user", "content": "Run a connection check."},
            ],
            "thinking": {"type": "disabled"},
        }
        try:
            response = self._request_post(
                f"{config.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=max(15, config.request_timeout * 2),
            )
        except requests.RequestException as exc:
            raise LLMTemporaryError("暂时无法连接模型服务，请检查 URL 或稍后重试。") from exc
        except Exception as exc:
            raise LLMTemporaryError("暂时无法连接模型服务，请稍后重试。") from exc

        status_code = int(getattr(response, "status_code", 0))
        if status_code in {401, 403}:
            raise LLMAuthenticationError("模型服务拒绝了 API Key，请更新后重试。")
        if status_code == 429:
            raise LLMTemporaryError("模型服务暂时限流，请稍后重试。")
        if status_code == 404:
            raise LLMConfigurationError("模型服务地址或模型名称不可用，请检查后重试。")
        if status_code < 200 or status_code >= 300:
            raise LLMTemporaryError(f"模型服务返回 HTTP {status_code}，请稍后重试。")
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise LLMResponseError("模型服务返回的内容不兼容 Chat Completions 接口。") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("模型服务没有返回有效的测试结果。")
