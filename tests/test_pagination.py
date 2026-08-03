from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.storage.database import Database
from app.storage.repository import Repository, iso_now


class PaginationTests(unittest.TestCase):
    def test_event_query_never_returns_more_than_fifty(self) -> None:
        with TemporaryDirectory() as directory:
            repository = Repository(Database(Path(directory) / "test.db"))
            repository.database.initialize()
            with repository.database.transaction() as conn:
                now = iso_now()
                for index in range(55):
                    conn.execute(
                        """
                        INSERT INTO events (
                            fingerprint, title, importance_score, first_seen_at, last_seen_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (f"event-{index}", f"事件 {index}", index, now, now, now, now),
                    )
            self.assertEqual(repository.count_events(period="all"), 55)
            self.assertEqual(len(repository.list_events(period="all", limit=100)), 50)
