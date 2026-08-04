from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.domain.models import FeedItem, SourceDraft, SourceKind
from app.storage.database import Database
from app.storage.repository import Repository


class MediaRepositoryTests(unittest.TestCase):
    def test_refetch_backfills_media_without_reprocessing_the_item(self) -> None:
        with TemporaryDirectory() as directory:
            database = Database(Path(directory) / "media.db")
            database.initialize()
            repository = Repository(database)
            source_id = repository.create_source(
                SourceDraft(
                    name="媒体测试源",
                    kind=SourceKind.RSS,
                    locator="https://example.test/feed.xml",
                ),
                "https://example.test/feed.xml",
            )
            item = FeedItem(
                guid="same-item",
                title="同一篇资讯",
                link="https://example.test/post",
                content="同一段正文",
            )

            item_id, inserted = repository.insert_item(source_id, item)
            self.assertTrue(inserted)

            item_id_again, inserted_again = repository.insert_item(
                source_id,
                FeedItem(
                    guid="same-item",
                    title="同一篇资讯",
                    link="https://example.test/post",
                    content="同一段正文",
                    media=[
                        {
                            "kind": "image",
                            "url": "https://cdn.example.test/preview.jpg",
                            "alt": "预览图",
                        }
                    ],
                ),
            )

            self.assertEqual(item_id_again, item_id)
            self.assertFalse(inserted_again)
            stored = repository.get_item(item_id)
            assert stored is not None
            self.assertEqual(
                stored["media"],
                [
                    {
                        "kind": "image",
                        "url": "https://cdn.example.test/preview.jpg",
                        "alt": "预览图",
                    }
                ],
            )
