"""经自建 RSSHub 获取 Reddit 社区与用户动态。"""

from __future__ import annotations

import re

from app.config import Settings
from app.domain.models import SourceKind
from app.plugins.rss import RssSourcePlugin


class RedditSourcePlugin(RssSourcePlugin):
    kind = SourceKind.REDDIT
    label = "Reddit 社区或用户（RSSHub）"

    def normalize_locator(self, locator: str) -> str:
        value = locator.strip().rstrip("/")
        value = re.sub(r"^https?://(?:www\.)?reddit\.com/", "", value, flags=re.IGNORECASE)
        value = value.replace("/submitted/.rss", "").replace("/.rss", "")
        value = value.lstrip("/")
        if not re.fullmatch(r"(?:r|u|user)/[A-Za-z0-9_]+", value, flags=re.IGNORECASE):
            raise ValueError("请输入 r/社区名、u/用户名，或对应的 Reddit 地址。")
        return value.lower()

    def resolve_feed_url(self, locator: str, settings: Settings) -> str:
        if not settings.rsshub_base_url:
            raise ValueError("请先在 config.yml 的 app.rsshub_base_url 填入已部署 RSSHub 的地址。")
        normalized = self.normalize_locator(locator)
        owner, name = normalized.split("/", 1)
        if owner == "r":
            return f"{settings.rsshub_base_url}/reddit/r/{name}"
        return f"{settings.rsshub_base_url}/reddit/u/{name}"
