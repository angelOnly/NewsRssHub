from __future__ import annotations

import unittest
from typing import Any

import requests

from app.plugins.reddit import RedditSourcePlugin


def http_error(status_code: int, retry_after: str | None = None) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return requests.HTTPError(f"HTTP {status_code}", response=response)


class StubRedditPlugin(RedditSourcePlugin):
    def __init__(self, outcomes: list[object], sleeps: list[float]) -> None:
        super().__init__(sleeper=sleeps.append)
        self.outcomes = outcomes
        self.fetch_calls: list[int] = []

    def fetch(self, source: dict[str, Any], settings: Any) -> list[Any]:
        self.fetch_calls.append(int(source["id"]))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return list(outcome)  # type: ignore[arg-type]


class RedditBatchTests(unittest.TestCase):
    def test_batch_spaces_requests_between_sources(self) -> None:
        sleeps: list[float] = []
        plugin = StubRedditPlugin([[], [], []], sleeps)

        results = plugin.fetch_many([{"id": 1}, {"id": 2}, {"id": 3}], object())

        self.assertTrue(all(result.error is None for result in results.values()))
        self.assertEqual(plugin.fetch_calls, [1, 2, 3])
        self.assertEqual(sleeps, [6.0, 6.0])

    def test_429_retries_once_after_retry_after_delay(self) -> None:
        sleeps: list[float] = []
        plugin = StubRedditPlugin([http_error(429, "9"), []], sleeps)

        result = plugin.fetch_many([{"id": 1}], object())[1]

        self.assertIsNone(result.error)
        self.assertEqual(plugin.fetch_calls, [1, 1])
        self.assertEqual(sleeps, [9.0])

    def test_permanent_http_error_is_not_retried(self) -> None:
        sleeps: list[float] = []
        payment_required = http_error(402)
        plugin = StubRedditPlugin([payment_required], sleeps)

        result = plugin.fetch_many([{"id": 1}], object())[1]

        self.assertIs(result.error, payment_required)
        self.assertEqual(plugin.fetch_calls, [1])
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
