from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config import Settings
from app.domain.models import SourceDraft, SourceKind
from app.plugins.registry import build_source_registry
from app.services.rsshub_runtime import RssHubRuntimeFiles
from app.services.sources import SourceService
from app.storage.database import Database
from app.storage.repository import Repository


def build_settings(root: Path) -> Settings:
    source_dir = root / "sources"
    source_dir.mkdir()
    return Settings(
        root_dir=root,
        source_dir=source_dir,
        data_dir=root / "data",
        database_path=root / "data" / "test.db",
        request_timeout=5,
        log_level="INFO",
        llm_enabled=False,
        openai_api_key=None,
        openai_base_url="https://example.test/v1",
        openai_model_name="test",
        credential_encryption_key=None,
        timezone="Asia/Shanghai",
        rsshub_base_url="https://rsshub.example.test",
    )


class RssHubRuntimeTests(unittest.TestCase):
    def test_source_migration_and_manifest_only_expose_managed_generic_feeds(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            generic_url = "https://feeds.example.test/news.xml"
            generic_id = repository.create_source(
                SourceDraft(name="Generic", kind=SourceKind.RSS, locator=generic_url), generic_url
            )
            x_id = repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="OpenAI"),
                "https://x.com/OpenAI",
            )
            reddit_id = repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.REDDIT, locator="r/openai"),
                "https://www.reddit.com/r/openai/.rss",
            )
            youtube_id = repository.create_source(
                SourceDraft(
                    name="YouTube",
                    kind=SourceKind.YOUTUBE,
                    locator="UCXZCJLdBC09xxGZ6gcdrc6A",
                ),
                "https://www.youtube.com/feeds/videos.xml?channel_id=UCXZCJLdBC09xxGZ6gcdrc6A",
            )
            service = SourceService(repository, build_source_registry(), settings)

            self.assertEqual(service.synchronize_rsshub_sources(), 4)

            feed_key = RssHubRuntimeFiles.rss_feed_key(generic_url)
            self.assertEqual(
                repository.get_source(generic_id)["feed_url"],
                f"https://rsshub.example.test/newsrsshub/rss/{feed_key}",
            )
            self.assertEqual(
                repository.get_source(x_id)["feed_url"],
                "https://rsshub.example.test/twitter/user/OpenAI",
            )
            self.assertEqual(
                repository.get_source(reddit_id)["feed_url"],
                "https://rsshub.example.test/reddit/r/openai",
            )
            self.assertEqual(
                repository.get_source(youtube_id)["feed_url"],
                "https://rsshub.example.test/youtube/channel/UCXZCJLdBC09xxGZ6gcdrc6A",
            )

            manifest = json.loads(
                (settings.data_dir / "rsshub-runtime" / "rss-feeds.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["feeds"], {feed_key: {"url": generic_url}})

            service.archive_source(generic_id)
            archived_manifest = json.loads(
                (settings.data_dir / "rsshub-runtime" / "rss-feeds.json").read_text(encoding="utf-8")
            )
            self.assertEqual(archived_manifest["feeds"], {})


if __name__ == "__main__":
    unittest.main()
