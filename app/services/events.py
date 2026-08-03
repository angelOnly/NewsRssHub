from __future__ import annotations

import re
from typing import Any

from app.storage.repository import Repository


def _compact(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value)
    return " ".join(value.split())


def event_fingerprint(title: str) -> str:
    compact = _compact(title)
    return compact[:280] or "untitled-event"


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", _compact(value)))


def _similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _fallback_summary(item: dict[str, Any]) -> str:
    text = (item.get("content") or "").strip()
    if not text:
        return item["title"]
    chunks = re.split(r"(?<=[。！？.!?])\s+", text)
    return " ".join(chunks[:2]).strip()[:420] or item["title"]


class EventService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def assign_item(self, item_id: int) -> int | None:
        item = self.repository.get_item(item_id)
        if not item or item["blacklisted"]:
            return None

        fingerprint = event_fingerprint(item["title"])
        event = self.repository.find_event_by_fingerprint(fingerprint)
        if not event:
            # Exact title matching does most of the work. A small, bounded fuzzy
            # pass catches syndicated headlines without creating a global NLP job.
            candidates = self.repository.recent_event_candidates(hours=72, limit=120)
            best = max(candidates, key=lambda candidate: _similarity(item["title"], candidate["title"]), default=None)
            if best and _similarity(item["title"], best["title"]) >= 0.78:
                event = best

        if event:
            event_id = int(event["id"])
            self.repository.attach_item_to_event(event_id, item_id)
        else:
            event_id = self.repository.create_event(
                fingerprint,
                {
                    **item,
                    "summary": _fallback_summary(item),
                    "why_matters": self._why_matters(item.get("tags", [])),
                },
            )
        self.repository.refresh_event(event_id)
        return event_id

    @staticmethod
    def _why_matters(tags: list[str]) -> str:
        if tags:
            return f"与关注方向「{'、'.join(tags[:3])}」直接相关，建议优先查看。"
        return "来源已收录，等待更多上下文判断其重要性。"
