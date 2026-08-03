"""Runtime-managed OpenAI-compatible model connection.

The API key lives in the same encrypted connector-credential store as the X
session.  Unlike environment-only configuration, this lets the web UI update
the model connection without exposing the key or restarting the worker.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings
from app.storage.repository import Repository


LLM_CONNECTOR = "llm_connection"


class LLMConnectionError(RuntimeError):
    """A user-safe model connection error that never includes an API key."""


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
    """The small, resolved configuration needed by the analyzer."""

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
    """Owns encrypted storage, validation, and dynamic resolution of LLM config."""

    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        request_post: Callable[..., Any] | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self._request_post = request_post or requests.post

    def status(self) -> LLMConnectionStatus:
        record = self.repository.get_connector_credential(LLM_CONNECTOR)
        if record:
            try:
                config = self._decrypt_record(record)
            except LLMConnectionError as exc:
                return LLMConnectionStatus(
                    state="error",
                    message=str(exc),
                    configured=False,
                    enabled=False,
                    source="database",
                    base_url="",
                    model_name="",
                    fingerprint=str(record.get("fingerprint") or ""),
                    updated_at=record.get("updated_at"),
                    last_validated_at=record.get("last_validated_at"),
                    last_error=str(record.get("last_error") or ""),
                )

            state = str(record.get("status") or "unknown")
            last_error = str(record.get("last_error") or "")
            messages = {
                "valid": "模型服务连接可用。",
                "invalid": "模型服务拒绝了当前 API Key，请更新后重试。",
                "error": last_error or "上次模型连接测试未通过，请稍后重试。",
            }
            return LLMConnectionStatus(
                state=state,
                message=messages.get(state, "模型连接尚未验证。"),
                configured=True,
                enabled=config.enabled,
                source="database",
                base_url=config.base_url,
                model_name=config.model_name,
                fingerprint=str(record.get("fingerprint") or ""),
                updated_at=record.get("updated_at"),
                last_validated_at=record.get("last_validated_at"),
                last_error=last_error,
            )

        try:
            file_config = self._file_config()
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
        if file_config:
            return LLMConnectionStatus(
                state="config",
                message="正在使用 config.yml 中的模型配置；可在此页验证或迁移为可在线维护的配置。",
                configured=True,
                enabled=file_config.enabled,
                source="config",
                base_url=file_config.base_url,
                model_name=file_config.model_name,
                fingerprint=_fingerprint(file_config.api_key),
            )
        if not self.settings.credential_encryption_key:
            return LLMConnectionStatus(
                state="needs_key",
                message="尚未设置凭据加密主密钥，无法安全保存模型 API Key。",
                configured=False,
                enabled=False,
                source="none",
                base_url=self.settings.openai_base_url,
                model_name=self.settings.openai_model_name,
            )
        return LLMConnectionStatus(
            state="missing",
            message="尚未配置模型 API Key，系统会使用本地规则摘要。",
            configured=False,
            enabled=self.settings.llm_enabled,
            source="none",
            base_url=self.settings.openai_base_url,
            model_name=self.settings.openai_model_name,
        )

    def runtime_config(self) -> LLMRuntimeConfig | None:
        record = self.repository.get_connector_credential(LLM_CONNECTOR)
        if record:
            try:
                return self._decrypt_record(record)
            except LLMConnectionError:
                return None
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
        current = self.runtime_config()
        api_key = api_key_value.strip() or (current.api_key if current else "")
        if not api_key:
            raise LLMCredentialMissingError("请填写模型 API Key。")

        candidate = LLMRuntimeConfig(
            api_key=api_key,
            base_url=_normalize_base_url(base_url or (current.base_url if current else self.settings.openai_base_url)),
            model_name=_normalize_model_name(model_name or (current.model_name if current else self.settings.openai_model_name)),
            enabled=enabled,
            source="database",
            request_timeout=self.settings.request_timeout,
        )
        # A failed candidate must never overwrite the configuration currently
        # used by the worker.
        self._test_config(candidate)
        self._save(candidate)
        return self.status()

    def test_saved(self) -> LLMConnectionStatus:
        config = self.runtime_config()
        if not config:
            raise LLMCredentialMissingError("尚未配置模型 API Key。")
        try:
            self._test_config(config)
        except LLMConnectionError as exc:
            self._record_failure(exc)
            raise
        record = self.repository.get_connector_credential(LLM_CONNECTOR)
        if record:
            self.repository.update_connector_credential_health(
                LLM_CONNECTOR, status="valid", last_error="", validated=True
            )
        return self.status()

    def _cipher(self) -> Fernet:
        raw_key = self.settings.credential_encryption_key
        if not raw_key:
            raise LLMConfigurationError("尚未设置 CREDENTIAL_ENCRYPTION_KEY，无法安全保存模型 API Key。")
        try:
            return Fernet(raw_key.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise LLMConfigurationError("CREDENTIAL_ENCRYPTION_KEY 格式无效。") from exc

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

    def _decrypt_record(self, record: dict[str, Any]) -> LLMRuntimeConfig:
        try:
            raw_payload = self._cipher().decrypt(str(record["ciphertext"]).encode("ascii"))
            payload = json.loads(raw_payload.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
            raise LLMConfigurationError("已保存的模型连接无法读取，请重新保存一次。") from exc
        if not isinstance(payload, dict):
            raise LLMConfigurationError("已保存的模型连接格式无效，请重新保存一次。")
        api_key = str(payload.get("api_key") or "")
        if not api_key:
            raise LLMConfigurationError("已保存的模型连接缺少 API Key，请重新保存一次。")
        return LLMRuntimeConfig(
            api_key=api_key,
            base_url=_normalize_base_url(str(payload.get("base_url") or "")),
            model_name=_normalize_model_name(str(payload.get("model_name") or "")),
            enabled=bool(payload.get("enabled", True)),
            source="database",
            request_timeout=self.settings.request_timeout,
        )

    def _save(self, config: LLMRuntimeConfig) -> None:
        ciphertext = self._cipher().encrypt(
            json.dumps(
                {
                    "api_key": config.api_key,
                    "base_url": config.base_url,
                    "model_name": config.model_name,
                    "enabled": config.enabled,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        self.repository.save_connector_credential(
            connector=LLM_CONNECTOR,
            ciphertext=ciphertext,
            fingerprint=_fingerprint(config.api_key),
            status="valid",
        )

    def _test_config(self, config: LLMRuntimeConfig) -> None:
        body = {
            "model": config.model_name,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Return a JSON object with one boolean field named ok."},
                {"role": "user", "content": "Run a connection check."},
            ],
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
            raise LLMTemporaryError("模型服务暂时无法完成请求，请稍后重试。")
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise LLMResponseError("模型服务返回的内容不兼容 Chat Completions 接口。") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("模型服务没有返回有效的测试结果。")

    def _record_failure(self, exc: LLMConnectionError) -> None:
        record = self.repository.get_connector_credential(LLM_CONNECTOR)
        if not record:
            return
        status = "invalid" if isinstance(exc, LLMAuthenticationError) else "error"
        self.repository.update_connector_credential_health(
            LLM_CONNECTOR, status=status, last_error=str(exc)
        )
