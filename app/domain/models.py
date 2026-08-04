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
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    feed_url: str
    message: str
    feed_title: str = ""
    item_count: int = 0
