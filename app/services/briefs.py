from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.domain.curation import EditorialTier
from app.storage.repository import Repository


class BriefService:
    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    def generate_today(self) -> dict[str, object]:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        must_read = self.repository.list_events(
            tier=EditorialTier.MUST_READ, period="24h", limit=12
        )
        important = self.repository.list_events(
            tier=EditorialTier.IMPORTANT, period="24h", limit=8
        )
        events = [*must_read, *important]
        if events:
            intro = (
                f"过去 24 小时收录 {len(must_read)} 条必看和 "
                f"{len(important)} 条重要更新；同一事件的重复消息已合并。"
            )
        else:
            intro = "过去 24 小时尚未出现必看或重要更新。"
        title = f"{now:%Y年%m月%d日} 每日情报"
        event_ids = [int(event["id"]) for event in events]
        self.repository.upsert_brief(now.date(), title, intro, event_ids)
        return {"date": now.date().isoformat(), "title": title, "intro": intro, "event_count": len(events)}
