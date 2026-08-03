from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.storage.repository import Repository


class BriefService:
    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    def generate_today(self) -> dict[str, object]:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        events = self.repository.list_events(period="24h", limit=20)
        if events:
            tags: list[str] = []
            for event in events:
                for tag in event.get("tags", []):
                    if tag not in tags:
                        tags.append(tag)
            intro = f"过去 24 小时共筛出 {len(events)} 个高相关事件，重点覆盖{'、'.join(tags[:4]) or '你的关注方向'}。"
        else:
            intro = "过去 24 小时尚未出现达到当前筛选阈值的高相关事件。"
        title = f"{now:%Y年%m月%d日} 每日情报"
        event_ids = [int(event["id"]) for event in events]
        self.repository.upsert_brief(now.date(), title, intro, event_ids)
        return {"date": now.date().isoformat(), "title": title, "intro": intro, "event_count": len(events)}
