from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config import Settings
from app.domain.curation import CurationGroup, EditorialTier
from app.domain.models import FeedItem, SourceDraft, SourceKind
from app.services.llm_connection import LLMRuntimeConfig
from app.services.translator import TranslationService
from app.storage.database import Database
from app.storage.repository import Repository


class FixedConnection:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def runtime_config(self) -> LLMRuntimeConfig:
        return LLMRuntimeConfig(
            api_key="test-key",
            base_url="https://llm.example.test/v1",
            model_name="test-model",
            enabled=self.enabled,
            source="test",
            request_timeout=5,
        )


class TranslationClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete_json(self, *, system: str, user: dict[str, object]) -> dict[str, object]:
        content = str(user["content"])
        self.calls.append(content)
        return {"translation": f"中文译文：{content}"}


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
        llm_enabled=True,
        openai_api_key=None,
        openai_base_url="https://llm.example.test/v1",
        openai_model_name="test-model",
        credential_encryption_key=None,
        timezone="Asia/Shanghai",
    )


class TranslationTests(unittest.TestCase):
    def test_important_primary_body_is_translated_once_and_cached(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            item_id, inserted = repository.insert_item(
                source_id,
                FeedItem(
                    guid="english",
                    title="A major new model is available",
                    link="https://example.test/model",
                    content="The model is now available to developers with a new API.",
                    published_at=datetime.now(timezone.utc),
                ),
            )
            self.assertTrue(inserted)
            repository.save_item_summary(
                item_id,
                display_title="新模型现已向开发者开放",
                summary="该模型已开放新的 API 使用方式。",
                highlights=["开发者已可通过新 API 使用。"],
                version=2,
            )
            repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[item_id],
                        primary_item_id=item_id,
                        tier=EditorialTier.IMPORTANT,
                        reason="已有可用的新 API 能力",
                        order=1,
                    )
                ]
            )

            client = TranslationClient()
            service = TranslationService(
                repository,
                settings,
                llm_connections=FixedConnection(),  # type: ignore[arg-type]
                client_factory=lambda _config: client,  # type: ignore[arg-type]
            )
            first = service.translate_visible_primary_items()
            item = repository.get_item(item_id)
            assert item is not None

            self.assertEqual(first.completed, 1)
            self.assertEqual(first.direct, 0)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(item["translation_status"], "complete")
            self.assertEqual(item["translated_content"], "中文译文：The model is now available to developers with a new API.")

            second = service.translate_visible_primary_items()
            self.assertEqual(second.completed, 0)
            self.assertEqual(len(client.calls), 1)

    def test_chinese_body_is_saved_without_model(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="中文来源", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            body = "该模型今天正式发布，已经可以通过 API 使用，并包含明确的迁移期限。"
            item_id, inserted = repository.insert_item(
                source_id,
                FeedItem(
                    guid="chinese",
                    title="中文标题",
                    link="https://example.test/chinese",
                    content=body,
                    published_at=datetime.now(timezone.utc),
                ),
            )
            self.assertTrue(inserted)

            service = TranslationService(
                repository,
                settings,
                llm_connections=FixedConnection(enabled=False),  # type: ignore[arg-type]
            )
            outcome = service.translate_item(item_id)
            item = repository.get_item(item_id)
            assert item is not None

            self.assertEqual(outcome, "direct")
            self.assertEqual(item["translated_content"], body)
            self.assertEqual(item["translation_status"], "complete")
