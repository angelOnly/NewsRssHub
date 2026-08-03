from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config import Settings
from app.domain.models import SourceDraft, SourceKind
from app.plugins.registry import build_source_registry
from app.services.sources import SourceService
from app.storage.database import Database
from app.storage.repository import Repository


class SourceServiceTests(unittest.TestCase):
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
                rsshub_base_url="https://rsshub.example.test",
                rsshub_exclude_paths=(),
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

            source, validation = service.add_source(
                SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="@OpenAI"),
                validate=False,
            )
            self.assertIsNone(validation)
            self.assertEqual(source["locator"], "OpenAI")
            self.assertEqual(source["feed_url"], "https://x.com/OpenAI")
