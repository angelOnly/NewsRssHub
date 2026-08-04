from __future__ import annotations

import re
import time
from typing import Any, Callable

import requests

from app.config import Settings
from app.domain.models import FeedItem, SourceKind
from app.plugins.base import SourceFetchResult
from app.plugins.rss import RssSourcePlugin


class RedditSourcePlugin(RssSourcePlugin):
    kind = SourceKind.REDDIT
    label = "Reddit 社区或用户"

    # Reddit's anonymous RSS endpoint applies a much tighter per-IP limit than
    # a single manual source test suggests. Keep batch requests deliberately
    # slow and retry a 429 once after the server's cooldown (when supplied).
    DEFAULT_BATCH_DELAY_SECONDS = 6.0
    DEFAULT_RATE_LIMIT_RETRY_SECONDS = 15.0
    MAX_RETRY_AFTER_SECONDS = 30.0

    def __init__(
        self,
        *,
        sleeper: Callable[[float], None] | None = None,
        batch_delay_seconds: float = DEFAULT_BATCH_DELAY_SECONDS,
        rate_limit_retry_seconds: float = DEFAULT_RATE_LIMIT_RETRY_SECONDS,
    ) -> None:
        self._sleep = sleeper or time.sleep
        self.batch_delay_seconds = max(0.0, batch_delay_seconds)
        self.rate_limit_retry_seconds = max(0.0, rate_limit_retry_seconds)

    def normalize_locator(self, locator: str) -> str:
        value = locator.strip().rstrip("/")
        value = re.sub(r"^https?://(?:www\.)?reddit\.com/", "", value, flags=re.IGNORECASE)
        value = value.replace("/submitted/.rss", "").replace("/.rss", "")
        value = value.lstrip("/")
        if not re.fullmatch(r"(?:r|u|user)/[A-Za-z0-9_]+", value, flags=re.IGNORECASE):
            raise ValueError("请输入 r/社区名、u/用户名，或对应的 Reddit 地址。")
        return value.lower()

    def resolve_feed_url(self, locator: str, settings: Settings) -> str:
        normalized = self.normalize_locator(locator)
        if normalized.startswith("r/"):
            return f"https://www.reddit.com/{normalized}/.rss"
        username = normalized.split("/", 1)[1]
        return f"https://www.reddit.com/user/{username}/submitted/.rss"

    def fetch(self, source: dict[str, Any], settings: Settings) -> list[FeedItem]:
        if not source.get("feed_url"):
            source = {**source, "feed_url": self.resolve_feed_url(source["locator"], settings)}
        return super().fetch(source, settings)

    def fetch_many(
        self,
        sources: list[dict[str, Any]],
        settings: Settings,
        *,
        wait_between: Callable[[], None] | None = None,
    ) -> dict[int, SourceFetchResult]:
        """Fetch a Reddit batch without turning a healthy endpoint into 429s."""

        results: dict[int, SourceFetchResult] = {}
        for index, source in enumerate(sources):
            if index:
                if wait_between:
                    wait_between()
                else:
                    self._sleep(self.batch_delay_seconds)

            source_id = int(source["id"])
            try:
                results[source_id] = SourceFetchResult(items=self.fetch(source, settings))
            except Exception as exc:
                if not self._is_rate_limited(exc):
                    results[source_id] = SourceFetchResult(error=exc)
                    continue

                self._sleep(self._retry_delay(exc))
                try:
                    results[source_id] = SourceFetchResult(items=self.fetch(source, settings))
                except Exception as retry_exc:
                    results[source_id] = SourceFetchResult(error=retry_exc)
        return results

    def _retry_delay(self, exc: Exception) -> float:
        response = exc.response if isinstance(exc, requests.HTTPError) else None
        raw_retry_after = response.headers.get("Retry-After", "") if response is not None else ""
        try:
            retry_after = float(raw_retry_after)
        except (TypeError, ValueError):
            retry_after = self.rate_limit_retry_seconds
        return max(0.0, min(retry_after, self.MAX_RETRY_AFTER_SECONDS))

    @staticmethod
    def _is_rate_limited(exc: Exception) -> bool:
        return (
            isinstance(exc, requests.HTTPError)
            and exc.response is not None
            and exc.response.status_code == 429
        )
