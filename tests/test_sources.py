from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config import Settings
from app.domain.models import SourceDraft, SourceKind
from app.plugins.registry import build_source_registry
from app.services.collector import Collector
from app.services.connections import ConnectionCatalog
from app.services.sources import SourceService
from app.storage.database import Database
from app.storage.repository import Repository


class SourceServiceTests(unittest.TestCase):
    def test_unconfigured_x_sources_are_not_fetched_or_marked_failed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                root_dir=root,
                source_dir=root / "sources",
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
            )
            database = Database(settings.database_path)
            database.initialize()
            repository = Repository(database)
            source_id = repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="OpenAI"),
                "https://x.com/OpenAI",
            )

            summary = Collector(
                repository,
                build_source_registry(),
                settings,
                ConnectionCatalog(),
            ).collect_due_sources()

            self.assertEqual(summary.sources_checked, 0)
            saved = repository.get_source(source_id)
            assert saved is not None
            self.assertEqual(saved["health_status"], "unknown")
            self.assertEqual(saved["last_error"], "")

    def test_enabled_platform_check_ignores_paused_and_archived_sources(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "data" / "test.db")
            repository = Repository(database)
            database.initialize()

            source_id = repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="OpenAI"),
                "https://x.com/OpenAI",
            )
            self.assertTrue(repository.has_enabled_source_kind(SourceKind.X_RSSHUB))

            repository.update_source(source_id, {"enabled": 0})
            self.assertFalse(repository.has_enabled_source_kind(SourceKind.X_RSSHUB))

            repository.update_source(source_id, {"enabled": 1})
            repository.archive_source(source_id)
            self.assertFalse(repository.has_enabled_source_kind(SourceKind.X_RSSHUB))

    def test_requeue_failed_platform_sources_marks_them_for_a_fresh_fetch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "data" / "test.db")
            repository = Repository(database)
            database.initialize()

            source_id = repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="OpenAI"),
                "https://x.com/OpenAI",
            )
            repository.update_source(
                source_id,
                {
                    "health_status": "error",
                    "last_fetch_at": "2026-08-03T00:00:00+00:00",
                    "last_error": "X 登录 Cookie 未配置",
                },
            )

            self.assertEqual(repository.requeue_failed_sources_for_kind(SourceKind.X_RSSHUB), 1)
            source = repository.get_source(source_id)
            assert source is not None
            self.assertEqual(source["health_status"], "unknown")
            self.assertEqual(source["last_error"], "")
            self.assertIsNone(source["last_fetch_at"])

    def test_manual_test_queue_only_resets_live_sources(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "data" / "test.db")
            database.initialize()
            repository = Repository(database)
            source_ids = [
                repository.create_source(
                    SourceDraft(name=name, kind=SourceKind.RSS, locator=f"https://example.test/{name}"),
                    f"https://example.test/{name}",
                )
                for name in ("live", "paused", "archived")
            ]
            for source_id in source_ids:
                repository.update_source(
                    source_id,
                    {
                        "health_status": "error",
                        "last_fetch_at": "2026-08-03T00:00:00+00:00",
                        "last_error": "fetch failed",
                    },
                )
            repository.update_source(source_ids[1], {"enabled": 0})
            repository.archive_source(source_ids[2])

            self.assertEqual(repository.requeue_sources_for_fetch(source_ids), 1)

            live = repository.get_source(source_ids[0])
            paused = repository.get_source(source_ids[1])
            archived = repository.get_source(source_ids[2])
            assert live is not None and paused is not None and archived is not None
            self.assertEqual(live["health_status"], "unknown")
            self.assertEqual(live["last_error"], "")
            self.assertIsNone(live["last_fetch_at"])
            self.assertEqual(paused["health_status"], "error")
            self.assertEqual(paused["last_error"], "fetch failed")
            self.assertEqual(archived["health_status"], "archived")
            self.assertEqual(archived["last_error"], "fetch failed")

    def test_auto_detection_and_configurable_source(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "sources"
            source_dir.mkdir()
            settings = Settings(
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
            )
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            service = SourceService(repository, build_source_registry(), settings)

            self.assertEqual(service.detect_kind("@OpenAI"), SourceKind.X_RSSHUB)
            self.assertEqual(service.detect_kind("r/comfyui"), SourceKind.REDDIT)
            self.assertEqual(service.detect_kind("https://example.test/feed.xml"), SourceKind.RSS)

            self.assertEqual(service.registry.get(SourceKind.X_RSSHUB).normalize_locator("@OpenAI"), "OpenAI")
            source, validation = service.add_source(
                SourceDraft(name="ComfyUI", kind=SourceKind.REDDIT, locator="r/comfyui"),
                validate=False,
            )
            self.assertIsNone(validation)
            self.assertEqual(source["locator"], "r/comfyui")
            self.assertEqual(source["feed_url"], "https://www.reddit.com/r/comfyui/.rss")
