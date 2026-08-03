from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.domain.models import FeedItem, SourceDraft, SourceKind
from app.services.events import EventService
from app.storage.database import Database
from app.storage.repository import Repository


class EventTests(unittest.TestCase):
    def test_paused_source_disappears_from_dashboard_queries_immediately(self) -> None:
        with TemporaryDirectory() as directory:
            repository = Repository(Database(Path(directory) / "test.db"))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="36Kr", kind=SourceKind.RSS, locator="https://example.test/36kr"),
                "https://example.test/36kr",
            )
            item_id, _ = repository.insert_item(
                source_id,
                FeedItem(
                    guid="36kr-1", title="不需要的泛行业资讯", link="https://example.test/36kr/1",
                    content="内容", published_at=datetime.now(timezone.utc),
                ),
                80, ["AI产品与行业前沿"], False,
            )
            event_id = EventService(repository).assign_item(item_id)
            assert event_id is not None
            repository.update_source(source_id, {"health_status": "healthy"})
            self.assertEqual(repository.count_events(period="all"), 1)
            self.assertEqual(len(repository.list_events(period="all")), 1)
            self.assertEqual(repository.dashboard_stats()["event_count"], 1)

            repository.update_source(source_id, {"enabled": 0})
            self.assertEqual(repository.count_events(period="all"), 0)
            self.assertEqual(repository.list_events(period="all"), [])
            self.assertEqual(repository.get_events_by_ids([event_id]), [])
            self.assertEqual(repository.dashboard_stats(), {"source_count": 0, "healthy_count": 0, "event_count": 0})

    def test_not_interested_event_is_hidden_without_deleting_its_history(self) -> None:
        with TemporaryDirectory() as directory:
            repository = Repository(Database(Path(directory) / "test.db"))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="Test", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            item_id, _ = repository.insert_item(
                source_id,
                FeedItem(
                    guid="ignore-1", title="不想看的内容", link="https://example.test/ignore",
                    content="内容", published_at=datetime.now(timezone.utc),
                ),
                80, ["AI产品与行业前沿"], False,
            )
            event_id = EventService(repository).assign_item(item_id)
            assert event_id is not None

            repository.mark_event_not_interested(event_id)
            self.assertEqual(repository.list_events(period="all"), [])
            self.assertIsNotNone(repository.get_event(event_id))

    def test_dashboard_uses_the_model_headline_without_losing_raw_source_title(self) -> None:
        with TemporaryDirectory() as directory:
            repository = Repository(Database(Path(directory) / "test.db"))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="Google", kind=SourceKind.RSS, locator="https://example.test/google"),
                "https://example.test/google",
            )
            item_id, _ = repository.insert_item(
                source_id,
                FeedItem(
                    guid="google-1", title="Gemini Spark is rolling out", link="https://example.test/gemini",
                    content="Gemini Spark announcement", published_at=datetime.now(timezone.utc),
                ),
                80, ["AI产品与行业前沿"], False,
            )
            event_id = EventService(repository).assign_item(item_id)
            assert event_id is not None
            repository.save_analysis(
                event_id,
                provider="test",
                model="test",
                payload={"headline": "Gemini Spark 向更多 Google AI Pro 用户开放"},
            )

            event = repository.list_events(period="all")[0]
            self.assertEqual(event["display_title"], "Gemini Spark 向更多 Google AI Pro 用户开放")
            self.assertEqual(event["title"], "Gemini Spark is rolling out")

    def test_english_headline_gets_one_localization_retry(self) -> None:
        with TemporaryDirectory() as directory:
            repository = Repository(Database(Path(directory) / "test.db"))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="Google", kind=SourceKind.RSS, locator="https://example.test/google"),
                "https://example.test/google",
            )
            item_id, _ = repository.insert_item(
                source_id,
                FeedItem(
                    guid="google-2", title="Gemini Spark is rolling out", link="https://example.test/gemini-2",
                    content="Gemini Spark announcement", published_at=datetime.now(timezone.utc),
                ),
                80, ["AI产品与行业前沿"], False,
            )
            event_id = EventService(repository).assign_item(item_id)
            assert event_id is not None
            repository.save_analysis(event_id, provider="test", model="test", payload={"headline": "Gemini Spark"})

            self.assertEqual(repository.requeue_unlocalized_headlines(), 1)
            self.assertEqual(repository.list_pending_events()[0]["id"], event_id)
            repository.save_analysis(
                event_id,
                provider="test",
                model="test",
                payload={"headline": "Gemini Spark 向更多用户开放"},
            )
            self.assertEqual(repository.requeue_unlocalized_headlines(), 0)

    def test_duplicate_headline_becomes_one_event(self) -> None:
        with TemporaryDirectory() as directory:
            repository = Repository(Database(Path(directory) / "test.db"))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="Test", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            first_id, first_new = repository.insert_item(
                source_id,
                FeedItem(guid="a", title="OpenAI releases a new model", link="https://example.test/a", content="first", published_at=datetime.now(timezone.utc)),
                70, ["AI产品与行业前沿"], False,
            )
            second_id, second_new = repository.insert_item(
                source_id,
                FeedItem(guid="b", title="OpenAI releases a new model", link="https://example.test/b", content="second", published_at=datetime.now(timezone.utc)),
                75, ["AI产品与行业前沿"], False,
            )
            self.assertTrue(first_new and second_new)
            service = EventService(repository)
            first_event = service.assign_item(first_id)
            second_event = service.assign_item(second_id)
            self.assertEqual(first_event, second_event)
            event = repository.get_event(int(first_event))
            self.assertEqual(len(event["items"]), 2)
