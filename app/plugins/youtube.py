"""Public YouTube channel RSS connector.

The project deliberately uses the user's RSSHub instance rather than an
unreliable public YouTube endpoint. The web form can accept either a channel
id, a ``/channel/UC...`` URL, or an official ``@handle``; handles are resolved
once during source setup and the durable channel id is stored in SQLite.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import urlparse

import requests

from app.config import Settings
from app.domain.models import SourceKind
from app.plugins.rss import RssSourcePlugin, USER_AGENT


_CHANNEL_ID_RE = re.compile(r"UC[A-Za-z0-9_-]{22}$")
_HANDLE_RE = re.compile(r"@[A-Za-z0-9._-]{1,100}$")
_CHANNEL_ID_IN_PAGE_RE = re.compile(
    r'"(?:externalId|browseId)":"(UC[A-Za-z0-9_-]{22})"'
)


class YouTubeSourcePlugin(RssSourcePlugin):
    kind = SourceKind.YOUTUBE
    label = "YouTube 频道（公开 RSS）"

    def __init__(
        self,
        *,
        channel_resolver: Callable[[str, Settings], str] | None = None,
    ) -> None:
        self._channel_resolver = channel_resolver

    def normalize_locator(self, locator: str) -> str:
        value = locator.strip().rstrip("/")
        if _CHANNEL_ID_RE.fullmatch(value):
            return value

        if value.startswith(("http://", "https://")):
            parsed = urlparse(value)
            host = parsed.netloc.casefold().removeprefix("www.").removeprefix("m.")
            if host != "youtube.com":
                raise ValueError("请粘贴 YouTube 频道主页、/channel/UC… 地址或频道 ID。")
            path = parsed.path.rstrip("/")
            if path.startswith("/channel/"):
                channel_id = path.split("/", 2)[2] if path.count("/") >= 2 else ""
                if _CHANNEL_ID_RE.fullmatch(channel_id):
                    return channel_id
            if path.startswith("/@"):
                value = path[1:]
            else:
                raise ValueError("YouTube 地址需要是 @频道主页或 /channel/UC… 地址。")

        if _HANDLE_RE.fullmatch(value):
            return value.casefold()
        raise ValueError("请填 @频道名、YouTube 频道 URL 或以 UC 开头的频道 ID。")

    def prepare_source(self, locator: str, settings: Settings) -> tuple[str, str]:
        normalized = self.normalize_locator(locator)
        channel_id = normalized if _CHANNEL_ID_RE.fullmatch(normalized) else self._resolve_handle(
            normalized, settings
        )
        return channel_id, self.resolve_feed_url(channel_id, settings)

    def resolve_feed_url(self, locator: str, settings: Settings) -> str:
        channel_id = self.normalize_locator(locator)
        if not _CHANNEL_ID_RE.fullmatch(channel_id):
            channel_id = self._resolve_handle(channel_id, settings)
        if not settings.rsshub_base_url:
            raise ValueError(
                "请先在 config.yml 的 app.rsshub_base_url 填入已部署 RSSHub 的可访问地址。"
            )
        return f"{settings.rsshub_base_url}/youtube/channel/{channel_id}"

    def _resolve_handle(self, handle: str, settings: Settings) -> str:
        if self._channel_resolver:
            channel_id = self._channel_resolver(handle, settings)
        else:
            response = requests.get(
                f"https://www.youtube.com/{handle}",
                timeout=settings.request_timeout,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"},
            )
            response.raise_for_status()
            match = _CHANNEL_ID_IN_PAGE_RE.search(response.text)
            if not match:
                raise ValueError(
                    "无法识别这个 YouTube 频道，请改粘贴频道主页中的 /channel/UC… 地址。"
                )
            channel_id = match.group(1)

        if not _CHANNEL_ID_RE.fullmatch(channel_id):
            raise ValueError("无法识别有效的 YouTube 频道 ID。")
        return channel_id
