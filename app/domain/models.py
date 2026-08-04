from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class SourceKind(StrEnum):
    RSS = "rss"
    X_RSSHUB = "x_rsshub"
    REDDIT = "reddit"
    YOUTUBE = "youtube"


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    """全局抓取节奏；来源不再分别决定轮询频率。"""

    interval_minutes: int = 60
    jitter_min_seconds: int = 60
    jitter_max_seconds: int = 300


@dataclass(slots=True)
class SourceDraft:
    name: str
    kind: SourceKind
    locator: str
    is_official: bool = False
    poll_interval_minutes: int = 60
    enabled: bool = True
    archived: bool = False


@dataclass(slots=True)
class FeedItem:
    guid: str
    title: str
    link: str
    content: str
    author: str = ""
    published_at: datetime | None = None
    # 仅保存已验证的远端媒体地址，抓取时不下载媒体文件。
    media: list[dict[str, str]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    feed_url: str
    message: str
    feed_title: str = ""
    item_count: int = 0
