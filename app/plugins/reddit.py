from __future__ import annotations

import re
from typing import Any

from app.config import Settings
from app.domain.models import FeedItem, SourceKind
from app.plugins.rss import RssSourcePlugin


class RedditSourcePlugin(RssSourcePlugin):
    kind = SourceKind.REDDIT
    label = "Reddit 社区或用户"

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
