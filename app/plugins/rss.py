"""Generic RSS/Atom source plugin and safe feed normalization helpers."""

from __future__ import annotations

import calendar
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from app.config import Settings
from app.domain.models import FeedItem, SourceKind
from app.plugins.base import SourcePlugin


USER_AGENT = "NewsRSSHub/1.0 (+local personal intelligence dashboard)"
MAX_MEDIA_PER_ENTRY = 12
IMAGE_SUFFIXES = (".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp")
VIDEO_SUFFIXES = (".m3u8", ".mp4", ".webm", ".mov", ".m4v", ".ogv")


def _safe_media_url(value: Any) -> str:
    """仅允许浏览器可安全加载的绝对 HTTP(S) 媒体地址。"""

    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return candidate


def _media_kind(url: str, media_type: Any = "") -> str:
    content_type = str(media_type or "").lower().split(";", 1)[0].strip()
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/") or content_type == "application/vnd.apple.mpegurl":
        return "video"
    path = urlparse(url).path.lower()
    if path.endswith(IMAGE_SUFFIXES):
        return "image"
    if path.endswith(VIDEO_SUFFIXES):
        return "video"
    return ""


def _first_media_attribute(element: Any, names: tuple[str, ...]) -> str:
    for name in names:
        value = element.get(name)
        if value:
            return str(value)
    srcset = str(element.get("srcset") or "").strip()
    if srcset:
        # srcset 最后一个候选通常是清晰度最高的版本。
        return srcset.split(",")[-1].strip().split()[0]
    return ""


def _append_media(
    media: list[dict[str, str]],
    seen: set[str],
    *,
    url: Any,
    media_type: Any = "",
    kind: str = "",
    poster_url: Any = "",
    alt: Any = "",
) -> None:
    if len(media) >= MAX_MEDIA_PER_ENTRY:
        return
    media_url = _safe_media_url(url)
    resolved_kind = kind or _media_kind(media_url, media_type)
    if not media_url or resolved_kind not in {"image", "video"}:
        return
    key = f"{resolved_kind}:{media_url}"
    if key in seen:
        return
    seen.add(key)
    asset = {"kind": resolved_kind, "url": media_url}
    clean_type = str(media_type or "").lower().split(";", 1)[0].strip()
    if clean_type:
        asset["mime_type"] = clean_type[:120]
    clean_poster = _safe_media_url(poster_url)
    if clean_poster:
        asset["poster_url"] = clean_poster
    clean_alt = clean_text(str(alt or ""))
    if clean_alt:
        asset["alt"] = clean_alt[:300]
    media.append(asset)


def _youtube_embed_url(value: Any) -> str:
    candidate = _safe_media_url(value)
    if not candidate:
        return ""
    parsed = urlparse(candidate)
    host = parsed.netloc.lower().removeprefix("www.").split(":", 1)[0]
    video_id = ""
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        path = parsed.path.strip("/")
        if path == "watch":
            video_id = (parse_qs(parsed.query).get("v") or [""])[0]
        elif path.startswith(("embed/", "shorts/", "live/")):
            video_id = path.split("/", 1)[1].split("/", 1)[0]
    if not video_id or not all(character.isalnum() or character in "-_" for character in video_id):
        return ""
    return f"https://www.youtube-nocookie.com/embed/{video_id}"


def _bilibili_embed_url(value: Any) -> str:
    candidate = _safe_media_url(value)
    if not candidate:
        return ""
    parsed = urlparse(candidate)
    host = parsed.netloc.lower().removeprefix("www.").split(":", 1)[0]
    if host not in {"bilibili.com", "m.bilibili.com"}:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "video":
        return ""
    video_id = parts[1]
    if not video_id.startswith("BV") or not all(character.isalnum() for character in video_id):
        return ""
    return f"https://player.bilibili.com/player.html?bvid={video_id}&page=1"


def _append_embed_media(media: list[dict[str, str]], seen: set[str], value: Any) -> None:
    source_url = _safe_media_url(value)
    if not source_url or len(media) >= MAX_MEDIA_PER_ENTRY:
        return
    provider = ""
    embed_url = _youtube_embed_url(source_url)
    if embed_url:
        provider = "YouTube"
    else:
        embed_url = _bilibili_embed_url(source_url)
        if embed_url:
            provider = "哔哩哔哩"
    if not embed_url:
        return
    key = f"embed:{embed_url}"
    if key in seen:
        return
    seen.add(key)
    media.append(
        {
            "kind": "embed",
            "url": embed_url,
            "source_url": source_url,
            "provider": provider,
        }
    )


def _extract_html_media(value: str, media: list[dict[str, str]], seen: set[str]) -> None:
    soup = BeautifulSoup(value or "", "html.parser")
    for meta in soup.find_all("meta"):
        property_name = str(meta.get("property") or meta.get("name") or "").lower()
        if property_name in {"og:image", "twitter:image"}:
            _append_media(media, seen, kind="image", url=meta.get("content") or "")
    for image in soup.find_all("img"):
        _append_media(
            media,
            seen,
            kind="image",
            url=_first_media_attribute(image, ("src", "data-src", "data-original", "data-lazy-src")),
            alt=image.get("alt") or image.get("title") or "",
        )
    for video in soup.find_all("video"):
        poster_url = video.get("poster") or ""
        _append_media(
            media,
            seen,
            kind="video",
            url=video.get("src") or "",
            media_type=video.get("type") or "",
            poster_url=poster_url,
        )
        for source in video.find_all("source"):
            _append_media(
                media,
                seen,
                kind="video",
                url=source.get("src") or "",
                media_type=source.get("type") or "",
                poster_url=poster_url,
            )
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        _append_media(media, seen, url=href, alt=anchor.get_text(" ", strip=True))
        _append_embed_media(media, seen, href)
    for frame in soup.find_all("iframe", src=True):
        _append_embed_media(media, seen, frame.get("src") or "")


def _extract_entry_media(entry: Any, content_html: str, entry_link: str) -> list[dict[str, str]]:
    """合并 RSS 媒体字段和正文标签，保留安全、可直接预览的地址。"""

    media: list[dict[str, str]] = []
    seen: set[str] = set()
    for asset in entry.get("media_content") or []:
        medium = str(asset.get("medium") or "").lower()
        _append_media(
            media,
            seen,
            url=asset.get("url") or asset.get("href") or "",
            media_type=asset.get("type") or "",
            kind=medium if medium in {"image", "video"} else "",
            poster_url=asset.get("thumbnail") or "",
        )
    for thumbnail in entry.get("media_thumbnail") or []:
        _append_media(media, seen, kind="image", url=thumbnail.get("url") or "")
    for enclosure in entry.get("enclosures") or []:
        _append_media(
            media,
            seen,
            url=enclosure.get("href") or enclosure.get("url") or "",
            media_type=enclosure.get("type") or "",
        )
    for link in entry.get("links") or []:
        if str(link.get("rel") or "").lower() == "enclosure":
            _append_media(
                media,
                seen,
                url=link.get("href") or "",
                media_type=link.get("type") or "",
            )
        _append_embed_media(media, seen, link.get("href") or "")
    _extract_html_media(content_html, media, seen)
    _append_embed_media(media, seen, entry_link)
    return media


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
        content_html = _entry_content(entry)
        content = clean_text(content_html)
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
                media=_extract_entry_media(entry, content_html, link),
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
