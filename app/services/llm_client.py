"""Small OpenAI-compatible JSON client shared by summarization and curation."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import requests

from app.services.llm_connection import LLMRuntimeConfig


class LLMRequestError(RuntimeError):
    """A safe operational error; it never includes a credential or response body."""


class OpenAICompatibleJsonClient:
    def __init__(
        self,
        config: LLMRuntimeConfig,
        request_post: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._request_post = request_post or requests.post

    def complete_json(self, *, system: str, user: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._request_post(
                f"{self.config.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.config.model_name,
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
                    ],
                },
                timeout=max(30, self.config.request_timeout * 2),
            )
        except requests.RequestException as exc:
            raise LLMRequestError("暂时无法连接模型服务，请稍后重试。") from exc
        except Exception as exc:
            raise LLMRequestError("模型服务请求失败，请稍后重试。") from exc

        status_code = int(getattr(response, "status_code", 0))
        if status_code in {401, 403}:
            raise LLMRequestError("模型服务拒绝了当前 API Key，请在设置中更新后重试。")
        if status_code == 429:
            raise LLMRequestError("模型服务暂时限流，稍后会自动重试。")
        if status_code < 200 or status_code >= 300:
            raise LLMRequestError("模型服务暂时无法完成请求，请稍后重试。")
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise LLMRequestError("模型服务没有返回兼容的 JSON 内容。") from exc
        if not isinstance(content, str):
            raise LLMRequestError("模型服务没有返回有效内容。")
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                raise LLMRequestError("模型服务没有返回有效 JSON。")
            try:
                result = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise LLMRequestError("模型服务没有返回有效 JSON。") from exc
        if not isinstance(result, dict):
            raise LLMRequestError("模型服务返回的 JSON 不是对象。")
        return result
