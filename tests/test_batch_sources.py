from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config import Settings
from app.domain.models import SourceDraft, SourceKind
from app.plugins.registry import build_source_registry
from app.plugins.youtube import YouTubeSourcePlugin
from app.services.batch_sources import BatchSourceImportService
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
        openai_base_url="https://llm.example.test/v1",
        openai_model_name="test-model",
        credential_encryption_key=None,
        timezone="Asia/Shanghai",
    )


class BatchSourceImportTests(unittest.TestCase):
    def test_x_accounts_can_be_saved_before_a_cookie_is_configured(self) -> None:
        with TemporaryDirectory() as directory:
            settings = replace(
                build_settings(Path(directory)), rsshub_base_url="https://rsshub.example.test"
            )
            database = Database(settings.database_path)
            database.initialize()
            repository = Repository(database)
            sources = SourceService(repository, build_source_registry(), settings)
            importer = BatchSourceImportService(sources)

            result = importer.import_text(
                kind=SourceKind.X_RSSHUB,
                entries="""
                    OpenAI | @OpenAI
                    Same account | https://x.com/OpenAI
                    Anthropic | @AnthropicAI
                    Bad input | not a valid handle!
                """,
                is_official=True,
            )

            self.assertEqual([row.locator for row in result.added], ["OpenAI", "AnthropicAI"])
            self.assertEqual(len(result.duplicates), 1)
            self.assertIn("重复", result.duplicates[0].message)
            self.assertEqual(len(result.errors), 1)
            self.assertEqual(repository.count_sources(), 2)
            saved = repository.find_source(SourceKind.X_RSSHUB.value, "OpenAI")
            assert saved is not None
            self.assertTrue(saved["enabled"])
            self.assertEqual(saved["health_status"], "unknown")

    def test_platform_paging_filters_and_counts_sources(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            database = Database(settings.database_path)
            database.initialize()
            repository = Repository(database)
            for index in range(25):
                repository.create_source(
                    SourceDraft(
                        name=f"RSS {index:02d}",
                        kind=SourceKind.RSS,
                        locator=f"https://example.test/{index}.xml",
                    ),
                    f"https://example.test/{index}.xml",
                )
            repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="OpenAI"),
                "https://x.com/OpenAI",
            )

            result = repository.list_sources_page(kind=SourceKind.RSS.value, page=2, page_size=20)

            self.assertEqual(result.total, 25)
            self.assertEqual(result.page, 2)
            self.assertEqual(result.page_count, 2)
            self.assertEqual(len(result.sources), 5)
            self.assertEqual(repository.source_kind_counts(), {"rss": 25, "x_rsshub": 1})


class YouTubeSourcePluginTests(unittest.TestCase):
    def test_handle_is_resolved_to_a_stable_channel_id_before_storage(self) -> None:
        with TemporaryDirectory() as directory:
            expected_id = "UCXZCJLdBC09xxGZ6gcdrc6A"
            plugin = YouTubeSourcePlugin(channel_resolver=lambda handle, settings: expected_id)
            settings = replace(
                build_settings(Path(directory)), rsshub_base_url="https://rsshub.example.test"
            )

            locator, feed_url = plugin.prepare_source("https://www.youtube.com/@OpenAI", settings)

            self.assertEqual(locator, expected_id)
            self.assertEqual(feed_url, f"https://rsshub.example.test/youtube/channel/{expected_id}")
            self.assertEqual(plugin.normalize_locator(f"https://www.youtube.com/channel/{expected_id}"), expected_id)

    def test_channel_id_requires_an_rsshub_address_before_batch_import(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            database = Database(settings.database_path)
            database.initialize()
            repository = Repository(database)
            sources = SourceService(repository, build_source_registry(), settings)
            importer = BatchSourceImportService(sources)

            result = importer.import_text(
                kind=SourceKind.YOUTUBE,
                entries="OpenAI | UCXZCJLdBC09xxGZ6gcdrc6A",
            )

            self.assertEqual(result.added, [])
            self.assertEqual(len(result.errors), 1)
            self.assertIn("rsshub_base_url", result.errors[0].message)
            self.assertEqual(repository.count_sources(), 0)
