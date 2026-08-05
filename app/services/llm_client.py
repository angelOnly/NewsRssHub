"""Small OpenAI-compatible JSON client shared by summarization and curation."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

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

    def complete_json(
        self,
        *,
        system: str,
        user: dict[str, Any],
        stream: bool = False,
        read_timeout: float | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """请求 OpenAI 兼容 JSON。

        流式模式用于独立的话题 Worker：模型持续输出时不受固定读超时限制，
        但仍会在最后统一校验完整 JSON。其他调用维持原有的普通请求超时。
        """

        request_payload = {
            "model": self.config.model_name,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
        }
        # 部分兼容网关通过请求体扩展字段控制思考模式；调用方显式决定是否传入。
        if extra_body:
            request_payload.update(extra_body)
        if stream:
            request_payload["stream"] = True
        # 非流式请求沿用通用上限。流式话题归并没有读超时；模型只要持续输出，
        # 独立 Worker 就继续接收，最后再把完整内容交给 JSON 校验。
        timeout: float | tuple[float, float | None]
        timeout = (
            (max(10, self.config.request_timeout), read_timeout)
            if stream
            else max(30, self.config.request_timeout * 2)
        )
        try:
            response = self._request_post(
                f"{self.config.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json=request_payload,
                timeout=timeout,
                **({"stream": True} if stream else {}),
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
            raise LLMRequestError(f"模型服务返回 HTTP {status_code}，请稍后重试。")
        content = self._streaming_content(response) if stream else self._response_content(response)
        return self._parse_json_content(content)

    @staticmethod
    def _response_content(response: Any) -> str:
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise LLMRequestError("模型服务没有返回兼容的 JSON 内容。") from exc
        if not isinstance(content, str):
            raise LLMRequestError("模型服务没有返回有效内容。")
        return content

    @staticmethod
    def _streaming_content(response: Any) -> str:
        """收集 OpenAI SSE 的 delta.content，兼容分片之间的空行和注释。"""

        chunks: list[str] = []
        try:
            # 不信任网关的 Content-Type 字符集声明；SSE JSON 统一按 UTF-8 解码，
            # 否则未带 charset 的 text/event-stream 会把中文话题名解成乱码。
            lines = response.iter_lines(decode_unicode=False)
            for raw_line in lines:
                if raw_line is None:
                    continue
                line = OpenAICompatibleJsonClient._stream_line_text(raw_line).strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                payload = json.loads(data)
                choices = payload.get("choices") if isinstance(payload, dict) else None
                # 某些兼容网关会插入没有 choices 的控制片段，和官方 SDK 一样跳过即可。
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                content = delta.get("content")
                if content is None:
                    # 少数兼容服务在流片段里仍使用 message 字段。
                    content = (choice.get("message") or {}).get("content")
                if content is not None:
                    if not isinstance(content, str):
                        raise TypeError("流式内容不是字符串")
                    chunks.append(content)
        except requests.RequestException as exc:
            raise LLMRequestError("模型服务的流式响应中断，请稍后重试。") from exc
        except (AttributeError, IndexError, KeyError, TypeError, UnicodeDecodeError, ValueError) as exc:
            raise LLMRequestError("模型服务没有返回兼容的流式 JSON 内容。") from exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        content = "".join(chunks)
        if not content:
            raise LLMRequestError("模型服务没有返回有效内容。")
        return content

    @staticmethod
    def _stream_line_text(raw_line: Any) -> str:
        """按 SSE 的 UTF-8 约定解码；测试桩或兼容适配器也可直接给 str。"""

        if isinstance(raw_line, bytes):
            return raw_line.decode("utf-8")
        if isinstance(raw_line, str):
            return raw_line
        raise TypeError("流式响应行不是文本")

    @staticmethod
    def _parse_json_content(content: str) -> dict[str, Any]:
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
