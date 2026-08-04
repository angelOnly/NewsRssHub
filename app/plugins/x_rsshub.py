"""经自建 RSSHub 获取 X 账号时间线。"""

from __future__ import annotations

import re

from app.config import Settings
from app.domain.models import SourceKind
from app.plugins.rss import RssSourcePlugin


class XRsshubSourcePlugin(RssSourcePlugin):
    """保留历史 kind 名称，实际抓取完全交给 RSSHub。"""

    kind = SourceKind.X_RSSHUB
    label = "X 账号（RSSHub）"

    def normalize_locator(self, locator: str) -> str:
        value = locator.strip().rstrip("/")
        value = re.sub(r"^https?://(?:www\.)?(?:x|twitter)\.com/", "", value, flags=re.IGNORECASE)
        value = value.lstrip("@").split("/", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", value):
            raise ValueError("请输入 X 用户名（例如 @OpenAI）或其主页地址。")
        return value

    def resolve_feed_url(self, locator: str, settings: Settings) -> str:
        if not settings.rsshub_base_url:
            raise ValueError("请先在 config.yml 的 app.rsshub_base_url 填入已部署 RSSHub 的地址。")
        return f"{settings.rsshub_base_url}/twitter/user/{self.normalize_locator(locator)}"
