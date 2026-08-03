"""Deterministic relevance scoring before any LLM call."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.domain.models import ScoreResult


def _clean_topic_name(value: str) -> str:
    return re.sub(r"^[^\w\u4e00-\u9fff]+\s*", "", value).strip()


def _interest_keywords(interest: dict[str, Any], taxonomy: dict[str, Any]) -> list[str]:
    explicit = interest.get("keywords") or []
    if explicit:
        return [str(keyword) for keyword in explicit]
    topic_name = _clean_topic_name(str(interest.get("name", "")))
    topics = taxonomy.get("topics", {})
    if topic_name in topics:
        return [str(keyword) for keyword in topics[topic_name].get("keywords", [])]
    # A tolerant fallback makes emoji-prefixed YAML labels work.
    for name, config in topics.items():
        if name in topic_name or topic_name in name:
            return [str(keyword) for keyword in config.get("keywords", [])]
    return []


def _age_bonus(published_at: datetime | None) -> float:
    if published_at is None:
        return 2.0
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    hours = max(0.0, (datetime.now(timezone.utc) - published_at.astimezone(timezone.utc)).total_seconds() / 3600)
    if hours <= 6:
        return 12.0
    if hours <= 24:
        return 9.0
    if hours <= 72:
        return 5.0
    if hours <= 168:
        return 2.0
    return 0.0


def score_item(
    *,
    title: str,
    content: str,
    published_at: datetime | None,
    source_priority: int,
    is_official: bool,
    profile: dict[str, Any],
    taxonomy: dict[str, Any],
) -> ScoreResult:
    """Return a bounded score with explainable topic tags.

    Rules intentionally decide *selection*. The model only explains selected
    events, keeping ranking predictable and inexpensive.
    """

    searchable = f"{title}\n{content}".casefold()
    title_folded = title.casefold()
    blacklist = [str(word).casefold() for word in profile.get("blacklist", [])]
    if any(word and word in searchable for word in blacklist):
        return ScoreResult(score=0.0, tags=[], is_blacklisted=True)

    score = max(0, min(int(source_priority), 10)) * 3.0
    if is_official:
        score += 10.0
    score += _age_bonus(published_at)

    tags: list[str] = []
    for interest in profile.get("interests", []):
        if not isinstance(interest, dict):
            continue
        keywords = _interest_keywords(interest, taxonomy)
        if not keywords:
            continue
        title_hits = sum(1 for keyword in keywords if keyword.casefold() in title_folded)
        body_hits = sum(1 for keyword in keywords if keyword.casefold() in searchable)
        if not (title_hits or body_hits):
            continue
        weight = max(1, min(int(interest.get("weight", 5)), 10))
        hit_score = min(20.0, title_hits * 5.0 + body_hits * 2.0)
        if weight >= 8:
            hit_score *= float(profile.get("scoring", {}).get("high_weight_multiplier", 1.5))
        score += hit_score * (weight / 10)
        tags.append(_clean_topic_name(str(interest.get("name", "未分类"))))

    if len(tags) > 1:
        score += float(profile.get("scoring", {}).get("multi_dimension_bonus", 2)) * (len(tags) - 1)

    return ScoreResult(score=round(min(score, 100.0), 1), tags=tags)
