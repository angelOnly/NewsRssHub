from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from app.config import Settings
from app.domain.models import SourceDraft, SourceKind
from app.plugins.registry import build_source_registry
from app.services.batch_sources import BatchSourceImportService
from app.services.source_backups import SourceBackupService
from app.services.sources import SourceService
from app.storage.database import Database
from app.storage.repository import Repository


def build_settings(root: Path, database_name: str = "test.db") -> Settings:
    source_dir = root / "sources"
    source_dir.mkdir()
    return Settings(
        root_dir=root,
        source_dir=source_dir,
        data_dir=root / "data",
        database_path=root / "data" / database_name,
        request_timeout=5,
        log_level="INFO",
        llm_enabled=False,
        openai_api_key=None,
        openai_base_url="https://llm.example.test/v1",
        openai_model_name="test-model",
        credential_encryption_key=None,
        timezone="Asia/Shanghai",
        rsshub_base_url="https://rsshub.example.test",
    )


class SourceBackupTests(unittest.TestCase):
    def test_export_can_be_uploaded_back_without_credentials_or_runtime_data(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = build_settings(root)
            database = Database(settings.database_path)
            database.initialize()
            repository = Repository(database)

            x_source_id = repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="OpenAI"),
                "https://x.com/OpenAI?not_for_export=true",
            )
            repository.update_source_config(
                x_source_id,
                {"x_user_id": "private-connector-cache-value"},
            )
            repository.create_source(
                SourceDraft(
                    name="暂停的 Reddit",
                    kind=SourceKind.REDDIT,
                    locator="r/machinelearning",
                    enabled=False,
                    poll_interval_minutes=180,
                ),
                "https://www.reddit.com/r/machinelearning/.rss",
            )
            repository.create_source(
                SourceDraft(
                    name="归档 RSS",
                    kind=SourceKind.RSS,
                    locator="https://example.test/archive.xml",
                    archived=True,
                ),
                "https://example.test/archive.xml?private=true",
            )
            repository.create_source(
                SourceDraft(
                    name="YouTube 测试频道",
                    kind=SourceKind.YOUTUBE,
                    locator="UCXZCJLdBC09xxGZ6gcdrc6A",
                    is_official=True,
                    poll_interval_minutes=360,
                ),
                "https://rsshub.example.test/youtube/channel/UCXZCJLdBC09xxGZ6gcdrc6A",
            )

            backups = SourceBackupService(repository, settings)
            exported = backups.export_text(
                now=datetime(2026, 8, 4, 9, 30, tzinfo=timezone.utc)
            )
            document = yaml.safe_load(exported)
            self.assertEqual(document["version"], 1)
            self.assertEqual(len(document["sources"]), 4)
            self.assertNotIn("private-connector-cache-value", exported)
            self.assertNotIn("not_for_export", exported)
            self.assertNotIn("private=true", exported)
            self.assertNotIn("feed_url", exported)
            self.assertNotIn("config_json", exported)

            restore_root = root / "restore"
            restore_root.mkdir()
            restore_settings = build_settings(restore_root, "restore.db")
            restore_database = Database(restore_settings.database_path)
            restore_database.initialize()
            restore_repository = Repository(restore_database)
            importer = BatchSourceImportService(
                SourceService(restore_repository, build_source_registry(), restore_settings)
            )
            result = importer.import_yaml(exported)

            self.assertEqual(len(result.added), 4)
            self.assertEqual(result.errors, [])
            paused = restore_repository.find_source(SourceKind.REDDIT.value, "r/machinelearning")
            archived = restore_repository.find_source(
                SourceKind.RSS.value, "https://example.test/archive.xml"
            )
            youtube = restore_repository.find_source(
                SourceKind.YOUTUBE.value, "UCXZCJLdBC09xxGZ6gcdrc6A"
            )
            assert paused is not None and archived is not None and youtube is not None
            self.assertFalse(paused["enabled"])
            self.assertFalse(paused["archived"])
            self.assertTrue(archived["archived"])
            self.assertFalse(archived["enabled"])
            self.assertEqual(archived["health_status"], "archived")
            self.assertTrue(youtube["is_official"])
            self.assertEqual(youtube["poll_interval_minutes"], 360)

    def test_periodic_backup_uses_its_timestamp_and_keeps_only_five_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = build_settings(root)
            database = Database(settings.database_path)
            database.initialize()
            repository = Repository(database)
            repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="OpenAI"),
                "https://x.com/OpenAI",
            )
            backups = SourceBackupService(repository, settings)
            first_time = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)

            first = backups.ensure_periodic_backup(now=first_time)
            self.assertIsNotNone(first)
            self.assertIsNone(backups.ensure_periodic_backup(now=first_time + timedelta(days=2, hours=23)))
            second = backups.ensure_periodic_backup(now=first_time + timedelta(days=3))
            self.assertIsNotNone(second)

            created = [backups.create_backup(now=first_time + timedelta(days=index + 10)) for index in range(6)]
            saved = backups.list_backups()
            self.assertEqual(len(saved), backups.RETAIN_COUNT)
            self.assertFalse((backups.directory / created[0].filename).exists())
            self.assertEqual(saved[0].filename, created[-1].filename)
            self.assertEqual(saved[0].source_count, 1)
            self.assertIsNone(backups.get_backup("../rss_news.db"))
            self.assertIsNone(backups.get_backup("sources-not-a-timestamp.yml"))
