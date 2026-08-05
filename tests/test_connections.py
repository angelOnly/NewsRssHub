from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config import Settings
from app.domain.models import SourceDraft, SourceKind
from app.plugins.registry import build_source_registry
from app.services.connections import ConnectionCatalog, ConnectionRequiredError
from app.services.sources import SourceService
from app.services.x_session import XSessionService
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
        openai_base_url="https://api.example.test/v1",
        openai_model_name="test-model",
        credential_encryption_key=None,
        timezone="Asia/Shanghai",
        rsshub_base_url="https://rsshub.example.test",
    )


class ConnectionCatalogTests(unittest.TestCase):
    def test_x_source_is_blocked_until_a_complete_cookie_file_exists(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            x_sessions = XSessionService(settings, validator=lambda: None)
            catalog = ConnectionCatalog(x_sessions, settings.rsshub_base_url)
            sources = SourceService(
                repository,
                build_source_registry(),
                settings,
                catalog,
            )
            draft = SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="OpenAI")

            with self.assertRaises(ConnectionRequiredError):
                sources.add_source(draft, validate=False)
            self.assertEqual(repository.count_sources(), 0)

            x_sessions.save_from_web("auth_token=known-good-cookie; ct0=known-good-csrf")
            connection = catalog.ensure_source_ready(SourceKind.X_RSSHUB)
            self.assertTrue(connection.usable)
            source, _ = sources.add_source(draft, validate=False)
            self.assertEqual(source["locator"], "OpenAI")

    def test_public_platforms_do_not_request_unneeded_credentials(self) -> None:
        catalog = ConnectionCatalog(rsshub_base_url="https://rsshub.example.test")
        for kind in (SourceKind.REDDIT, SourceKind.RSS):
            connection = catalog.ensure_source_ready(kind)
            self.assertFalse(connection.requires_credentials)
            self.assertTrue(connection.usable)
            self.assertEqual(connection.state, "public")
