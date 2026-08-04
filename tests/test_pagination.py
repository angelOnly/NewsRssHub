from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.domain.curation import EditorialTier
from app.domain.models import SourceDraft, SourceKind
from app.storage.database import Database
from app.storage.repository import Repository, iso_now


class PaginationTests(unittest.TestCase):
    def test_event_query_never_returns_more_than_fifty(self) -> None:
        with TemporaryDirectory() as directory:
            repository = Repository(Database(Path(directory) / "test.db"))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="Test", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            with repository.database.transaction() as conn:
                now = iso_now()
                for index in range(55):
                    event_id = conn.execute(
                        """
                        INSERT INTO events (
                            title, summary, editorial_tier, tier_reason, curation_order,
                            curation_status, first_seen_at, last_seen_at
                        ) VALUES (?, ?, 'brief', '', ?, 'complete', ?, ?)
                        """,
                        (f"事件 {index}", "摘要", index, now, now),
                    ).lastrowid
                    item_id = conn.execute(
                        """
                        INSERT INTO items (
                            source_id, event_id, guid, title, fetched_at, summary, summary_status
                        ) VALUES (?, ?, ?, ?, ?, '摘要', 'complete')
                        """,
                        (source_id, event_id, f"item-{index}", f"事件 {index}", now),
                    ).lastrowid
                    conn.execute("UPDATE events SET primary_item_id = ? WHERE id = ?", (item_id, event_id))
            self.assertEqual(repository.count_events(tier=EditorialTier.BRIEF, period="all"), 55)
            self.assertEqual(len(repository.list_events(tier=EditorialTier.BRIEF, period="all", limit=100)), 50)
