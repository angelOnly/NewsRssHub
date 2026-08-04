from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.domain.curation import CurationGroup, EditorialTier
from app.domain.models import FeedItem, SourceDraft, SourceKind
from app.storage.database import Database
from app.storage.repository import Repository


class FavoritesRepositoryTests(unittest.TestCase):
    def _repository(self) -> tuple[TemporaryDirectory[str], Repository, int]:
        directory = TemporaryDirectory()
        repository = Repository(Database(Path(directory.name) / "test.db"))
        repository.database.initialize()
        source_id = repository.create_source(
            SourceDraft(name="测试来源", kind=SourceKind.RSS, locator="https://example.test/feed"),
            "https://example.test/feed",
        )
        return directory, repository, source_id

    @staticmethod
    def _event(
        repository: Repository,
        source_id: int,
        guid: str,
        title: str,
        published_at: datetime,
    ) -> tuple[int, int]:
        item_id, inserted = repository.insert_item(
            source_id,
            FeedItem(
                guid=guid,
                title=title,
                link=f"https://example.test/{guid}",
                content=f"{title} 的原始内容",
                published_at=published_at,
            ),
        )
        assert inserted
        repository.save_item_summary(item_id, summary=f"摘要：{title}")
        event_id = repository.apply_curation_groups(
            [
                CurationGroup(
                    item_ids=[item_id],
                    primary_item_id=item_id,
                    tier=EditorialTier.IMPORTANT,
                    reason="测试收藏保留",
                    order=1,
                )
            ]
        )[0]
        return event_id, item_id

    def test_saved_event_can_be_read_after_source_is_paused(self) -> None:
        directory, repository, source_id = self._repository()
        with directory:
            event_id, _ = self._event(
                repository,
                source_id,
                "saved",
                "稍后阅读的更新",
                datetime.now(timezone.utc),
            )
            repository.save_event(event_id)
            repository.update_source(source_id, {"enabled": 0})

            self.assertIsNone(repository.get_event(event_id))
            saved = repository.list_saved_events()
            self.assertEqual([event["id"] for event in saved], [event_id])
            self.assertTrue(saved[0]["user_saved"])
            self.assertIsNotNone(repository.get_event(event_id, include_inactive_sources=True))

            repository.unsave_event(event_id)
            self.assertEqual(repository.list_saved_events(), [])

    def test_retention_preserves_saved_and_recent_brief_references(self) -> None:
        directory, repository, source_id = self._repository()
        with directory:
            now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
            old_time = now - timedelta(days=31)
            expired_event, expired_item = self._event(
                repository, source_id, "expired", "应删除的旧事件", old_time
            )
            saved_event, saved_item = self._event(
                repository, source_id, "saved-old", "收藏的旧事件", old_time
            )
            brief_event, brief_item = self._event(
                repository, source_id, "brief-old", "日报引用的旧事件", old_time
            )
            repository.save_event(saved_event)
            repository.upsert_brief(now.date(), "今日简报", "保留引用", [brief_event])
            repository.upsert_brief(
                now.date() - timedelta(days=31), "旧简报", "可删除", [expired_event]
            )

            outcome = repository.purge_expired_content(now)

            self.assertEqual(outcome, {"briefs": 1, "events": 1, "items": 1})
            self.assertIsNone(repository.get_event(expired_event, include_inactive_sources=True))
            self.assertIsNone(repository.get_item(expired_item))
            self.assertIsNotNone(repository.get_event(saved_event, include_inactive_sources=True))
            self.assertIsNotNone(repository.get_item(saved_item))
            self.assertTrue(repository.is_event_saved(saved_event))
            self.assertIsNotNone(repository.get_event(brief_event, include_inactive_sources=True))
            self.assertIsNotNone(repository.get_item(brief_item))


if __name__ == "__main__":
    unittest.main()
