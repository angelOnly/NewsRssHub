from __future__ import annotations

import unittest
from typing import Any

import requests

from app.services.llm_client import LLMRequestError, OpenAICompatibleJsonClient
from app.services.llm_connection import LLMRuntimeConfig


class JsonResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}


class StreamingResponse:
    status_code = 200

    def __init__(self, lines: list[str | bytes]) -> None:
        self.lines = lines
        self.closed = False
        self.decode_unicode: bool | None = None

    def iter_lines(self, *, decode_unicode: bool) -> list[str | bytes]:
        self.decode_unicode = decode_unicode
        return self.lines

    def close(self) -> None:
        self.closed = True


def build_config(request_timeout: int = 30) -> LLMRuntimeConfig:
    return LLMRuntimeConfig(
        api_key="test-key",
        base_url="https://api.example.test/v1",
        model_name="test-model",
        enabled=True,
        source="test",
        request_timeout=request_timeout,
    )


class OpenAICompatibleJsonClientTests(unittest.TestCase):
    def test_streaming_topic_request_collects_sse_without_read_timeout(self) -> None:
        response = StreamingResponse(
            [
                'data: {"choices":[{"delta":{"reasoning_content":"先判断事件关系"}}]}',
                'data: {"choices":[]}',
                b'data: {"choices":[{"delta":{"content":"{\\"topics\\":"}}]}',
                'data: {"choices":[{"delta":{"content":"[]}"}}]}',
                "",
                "data: [DONE]",
            ]
        )
        requests_seen: list[dict[str, Any]] = []

        def post(_url: str, **kwargs: Any) -> StreamingResponse:
            requests_seen.append(kwargs)
            return response

        client = OpenAICompatibleJsonClient(build_config(), request_post=post)
        result = client.complete_json(
            system="system",
            user={"events": []},
            stream=True,
            extra_body={"thinking": {"type": "disabled"}},
        )

        self.assertEqual(result, {"topics": []})
        self.assertEqual(len(requests_seen), 1)
        self.assertEqual(requests_seen[0]["timeout"], (30, None))
        self.assertTrue(requests_seen[0]["stream"])
        self.assertTrue(requests_seen[0]["json"]["stream"])
        self.assertEqual(requests_seen[0]["json"]["thinking"], {"type": "disabled"})
        self.assertNotIn("temperature", requests_seen[0]["json"])
        self.assertNotIn("top_p", requests_seen[0]["json"])
        self.assertNotIn("max_tokens", requests_seen[0]["json"])
        self.assertFalse(response.decode_unicode)
        self.assertTrue(response.closed)

    def test_regular_request_keeps_original_bounded_timeout(self) -> None:
        requests_seen: list[dict[str, Any]] = []

        def post(_url: str, **kwargs: Any) -> JsonResponse:
            requests_seen.append(kwargs)
            return JsonResponse()

        client = OpenAICompatibleJsonClient(build_config(), request_post=post)
        result = client.complete_json(system="system", user={"events": []})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(requests_seen[0]["timeout"], 60)
        self.assertNotIn("stream", requests_seen[0])
        self.assertNotIn("stream", requests_seen[0]["json"])
        self.assertNotIn("temperature", requests_seen[0]["json"])
        self.assertNotIn("top_p", requests_seen[0]["json"])
        self.assertNotIn("max_tokens", requests_seen[0]["json"])

    def test_stream_interruption_is_reported_as_safe_error(self) -> None:
        class BrokenResponse:
            status_code = 200

            def iter_lines(self, *, decode_unicode: bool) -> list[str]:
                raise requests.ConnectionError("connection interrupted")

            def close(self) -> None:
                pass

        client = OpenAICompatibleJsonClient(build_config(), request_post=lambda *_args, **_kwargs: BrokenResponse())

        with self.assertRaisesRegex(LLMRequestError, "流式响应中断"):
            client.complete_json(system="system", user={"events": []}, stream=True)
