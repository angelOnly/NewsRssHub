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
