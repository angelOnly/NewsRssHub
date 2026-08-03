"""Generic RSS/Atom source plugin and safe feed normalization helpers."""

from __future__ import annotations

import calendar
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import feedparser
import requests
from bs4 import BeautifulSoup

from app.config import Settings
from app.domain.models import FeedItem, SourceKind
from app.plugins.base import SourcePlugin


USER_AGENT = "NewsRSSHub/1.0 (+local personal intelligence dashboard)"


def clean_text(value: str) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def _entry_time(entry: Any) -> datetime | None:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


def _entry_content(entry: Any) -> str:
    content = entry.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            return str(first.get("value", ""))
    return str(entry.get("summary") or entry.get("description") or "")


def parse_feed_payload(payload: bytes, source_name: str = "") -> tuple[str, list[FeedItem]]:
    """Parse RSS/Atom bytes; exposed separately for deterministic tests."""

    parsed = feedparser.parse(payload)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        exception = getattr(parsed, "bozo_exception", "未知 RSS 格式错误")
        raise ValueError(f"RSS 解析失败：{exception}")

    feed_title = str(parsed.feed.get("title") or source_name or "未命名来源")
    items: list[FeedItem] = []
    for entry in parsed.entries:
        title = clean_text(str(entry.get("title") or "未命名内容"))
        link = str(entry.get("link") or "")
        content = clean_text(_entry_content(entry))
        raw_guid = str(entry.get("id") or entry.get("guid") or link or title)
        guid = hashlib.sha256(raw_guid.encode("utf-8", "ignore")).hexdigest()
        items.append(
            FeedItem(
                guid=guid,
                title=title[:500],
                link=link,
                content=content[:20000],
                author=clean_text(str(entry.get("author") or ""))[:300],
                published_at=_entry_time(entry),
                raw={"id": raw_guid, "link": link, "title": title},
            )
        )
    return feed_title, items


def fetch_feed(feed_url: str, timeout: int, source_name: str = "") -> tuple[str, list[FeedItem]]:
    response = requests.get(
        feed_url,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
    )
    response.raise_for_status()
    return parse_feed_payload(response.content, source_name)


class RssSourcePlugin(SourcePlugin):
    kind = SourceKind.RSS
    label = "RSS / Atom 地址"

    def normalize_locator(self, locator: str) -> str:
        candidate = locator.strip()
        if not candidate.startswith(("http://", "https://")):
            raise ValueError("RSS 地址必须以 http:// 或 https:// 开头。")
        return candidate

    def resolve_feed_url(self, locator: str, settings: Settings) -> str:
        return self.normalize_locator(locator)

    def fetch(self, source: dict[str, Any], settings: Settings) -> list[FeedItem]:
        _, items = fetch_feed(source["feed_url"], settings.request_timeout, source.get("name", ""))
        return items
