from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config import Settings
from app.domain.curation import CurationGroup, EditorialTier
from app.domain.models import FeedItem, SourceDraft, SourceKind
from app.services.curator import CurationService
from app.services.llm_connection import LLMRuntimeConfig
from app.services.summarizer import SummaryService
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


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def complete_json(self, *, system: str, user: dict[str, object]) -> dict[str, object]:
        self.calls.append(user)
        items = user["items"]
        assert isinstance(items, list)
        ids = [int(item["id"]) for item in items if isinstance(item, dict)]
        if len(ids) == 3:
            return {
                "groups": [
                    {
                        "item_ids": ids[:2],
                        "primary_item_id": ids[1],
                        "tier": "must_read",
                        "reason": "顶级模型已经进入可用工作流",
                        "order": 1,
                    },
                    {
                        "item_ids": [ids[2]],
                        "primary_item_id": ids[2],
                        "tier": "brief",
                        "reason": "普通素材更新",
                        "order": 1,
                    },
                ]
            }
        return {
            "groups": [
                {
                    "item_ids": [item_id],
                    "primary_item_id": item_id,
                    "tier": "must_read" if position == 0 else "brief",
                    "reason": "全局批次保持原判断",
                    "order": position + 1,
                }
                for position, item_id in enumerate(ids)
            ]
        }


class FailingCrossBatchClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, *, system: str, user: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("temporary cross-batch failure")
        items = user["items"]
        assert isinstance(items, list)
        return {
            "groups": [
                {
                    "item_ids": [int(item["id"])],
                    "primary_item_id": int(item["id"]),
                    "tier": "must_read" if index == 0 else "brief",
                    "reason": "test curation",
                    "order": index + 1,
                }
                for index, item in enumerate(items)
                if isinstance(item, dict)
            ]
        }


class SummaryArtifactClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def complete_json(self, *, system: str, user: dict[str, object]) -> dict[str, object]:
        self.calls.append({"system": system, "user": user})
        return {
            "title_zh": "MiniMax H3 已支持在 RTX 3060 本地运行",
            "summary": "MiniMax H3 已发布并获得 ComfyUI 原生支持。"
            "社区实测显示，12GB 显存的 RTX 3060 可在本地生成 480p 视频。",
            "highlights": [
                "已获得 ComfyUI 原生支持。",
                "12GB 显存 RTX 3060 可本地生成 480p 视频。",
            ],
        }


class OversizedSummaryArtifactClient:
    def complete_json(self, *, system: str, user: dict[str, object]) -> dict[str, object]:
        return {
            "title_zh": "标" * 51,
            "summary": "摘" * 221,
            "highlights": [],
        }


def build_settings(root: Path) -> Settings:
    source_dir = root / "sources"
    source_dir.mkdir()
    (source_dir / "user_profile.yml").write_text(
        "identity:\n  description: |\n    我关注大模型发布、ComfyUI 视频和个人开发工具。\n",
        encoding="utf-8",
    )
    skill = root / ".agents" / "skills" / "curate-personal-news"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# test curation policy\n", encoding="utf-8")
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


class CurationTests(unittest.TestCase):
    def test_model_summary_persists_chinese_title_and_key_facts(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="Reddit", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            item_id, inserted = repository.insert_item(
                source_id,
                FeedItem(
                    guid="minimax",
                    title="Open-weight video model Minimax H3 can now run locally",
                    link="https://example.test/minimax",
                    content="MiniMax H3 is now natively supported in ComfyUI on an RTX 3060.",
                    published_at=datetime.now(timezone.utc),
                ),
            )
            self.assertTrue(inserted)

            client = SummaryArtifactClient()
            service = SummaryService(
                repository,
                settings,
                llm_connections=FixedConnection(),  # type: ignore[arg-type]
                client_factory=lambda _config: client,  # type: ignore[arg-type]
            )
            result = service.summarize_pending()
            item = repository.get_item(item_id)
            assert item is not None

            self.assertEqual(result.completed, 1)
            self.assertEqual(item["display_title"], "MiniMax H3 已支持在 RTX 3060 本地运行")
            self.assertEqual(item["summary_version"], 2)
            self.assertEqual(
                item["highlights"],
                ["已获得 ComfyUI 原生支持。", "12GB 显存 RTX 3060 可本地生成 480p 视频。"],
            )
            self.assertIn("不超过50字", str(client.calls[0]["system"]))
            self.assertIn("约200字、最多220字", str(client.calls[0]["system"]))

            event_id = repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[item_id],
                        primary_item_id=item_id,
                        tier=EditorialTier.MUST_READ,
                        reason="已可在本地工作流使用",
                        order=1,
                    )
                ]
            )[0]
            event = repository.get_event(event_id)
            assert event is not None
            self.assertEqual(event["title"], item["display_title"])
            self.assertEqual(event["highlights"], item["highlights"])

    def test_model_summary_output_is_bounded_to_title_fifty_and_summary_two_hundred_twenty(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            service = SummaryService(repository, settings)

            artifact = service._summarize_with_model(  # noqa: SLF001
                OversizedSummaryArtifactClient(),  # type: ignore[arg-type]
                {"title": "原始标题", "content": "原始正文"},
            )

            self.assertEqual(artifact.display_title, "标" * 50)
            self.assertEqual(artifact.summary, "摘" * 220)

    def test_cross_batch_failure_does_not_hide_completed_events(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="RSS", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            for index in range(2):
                item_id, inserted = repository.insert_item(
                    source_id,
                    FeedItem(
                        guid=f"cross-{index}",
                        title=f"cross item {index}",
                        link=f"https://example.test/cross-{index}",
                        content=f"cross body {index}",
                        published_at=datetime.now(timezone.utc),
                    ),
                )
                self.assertTrue(inserted)
                repository.save_item_summary(item_id, summary=f"cross summary {index}")

            client = FailingCrossBatchClient()
            curator = CurationService(
                repository,
                settings,
                llm_connections=FixedConnection(),  # type: ignore[arg-type]
                client_factory=lambda _config: client,  # type: ignore[arg-type]
            )
            result = curator.curate_available(limit=10)

            self.assertEqual(result.completed, 2)
            self.assertEqual(client.calls, 2)
            self.assertEqual(
                len(repository.list_events(tier=EditorialTier.MUST_READ, period="all")), 1
            )
            self.assertEqual(len(repository.list_events(tier=EditorialTier.BRIEF, period="all")), 1)

    def test_summary_then_skill_only_receives_title_summary_and_time(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="Official", kind=SourceKind.RSS, locator="https://example.test/feed", is_official=True),
                "https://example.test/feed",
            )
            item_ids: list[int] = []
            for index, title in enumerate(("Flux 3 进入 ComfyUI", "官方节点现已可用", "相机 LoRA 更新")):
                item_id, inserted = repository.insert_item(
                    source_id,
                    FeedItem(
                        guid=str(index),
                        title=title,
                        link=f"https://example.test/{index}",
                        content=f"{title}。这是完整原文，不应传给筛选 Skill。",
                        published_at=datetime.now(timezone.utc),
                    ),
                )
                assert inserted
                repository.save_item_summary(item_id, summary=f"摘要：{title}")
                item_ids.append(item_id)

            client = RecordingClient()
            curator = CurationService(
                repository,
                settings,
                llm_connections=FixedConnection(),  # type: ignore[arg-type]
                client_factory=lambda _config: client,  # type: ignore[arg-type]
            )
            result = curator.curate_available(limit=10)

            self.assertEqual(result.completed, 3)
            self.assertGreaterEqual(len(client.calls), 1)
            first_items = client.calls[0]["items"]
            assert isinstance(first_items, list)
            self.assertEqual(set(first_items[0]), {"id", "title", "summary", "published_at"})
            self.assertNotIn("content", first_items[0])
            self.assertEqual(client.calls[0]["recent_feedback"], [])
            self.assertEqual(len(repository.list_events(tier=EditorialTier.MUST_READ, period="all")), 1)
            self.assertEqual(len(repository.list_events(tier=EditorialTier.BRIEF, period="all")), 1)
            event = repository.list_events(tier=EditorialTier.MUST_READ, period="all")[0]
            self.assertEqual(event["visible_item_count"], 2)
            self.assertEqual(event["visible_source_count"], 1)

    def test_skill_receives_recent_explicit_feedback_without_raw_body(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="RSS", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )

            read_id, inserted = repository.insert_item(
                source_id,
                FeedItem(
                    guid="read-feedback",
                    title="用户已阅读的模型评测",
                    link="https://example.test/read-feedback",
                    content="绝不能传给筛选 Skill 的已读原始正文。",
                    published_at=datetime.now(timezone.utc),
                ),
            )
            self.assertTrue(inserted)
            repository.save_item_summary(read_id, summary="用户已阅读的模型评测摘要。")
            read_event_id = repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[read_id],
                        primary_item_id=read_id,
                        tier=EditorialTier.IMPORTANT,
                        reason="测试近期已读反馈",
                        order=1,
                    )
                ]
            )[0]
            repository.mark_event_read(read_event_id)

            hidden_id, inserted = repository.insert_item(
                source_id,
                FeedItem(
                    guid="negative-feedback",
                    title="用户明确不感兴趣的营销消息",
                    link="https://example.test/negative-feedback",
                    content="绝不能传给筛选 Skill 的负反馈原始正文。",
                    published_at=datetime.now(timezone.utc),
                ),
            )
            self.assertTrue(inserted)
            repository.save_item_summary(hidden_id, summary="用户明确不感兴趣的营销消息摘要。")
            hidden_event_id = repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[hidden_id],
                        primary_item_id=hidden_id,
                        tier=EditorialTier.BRIEF,
                        reason="测试近期负反馈",
                        order=1,
                    )
                ]
            )[0]
            repository.mark_event_not_interested(hidden_event_id)

            pending_id, inserted = repository.insert_item(
                source_id,
                FeedItem(
                    guid="pending-curation",
                    title="待筛选的新消息",
                    link="https://example.test/pending-curation",
                    content="待筛选原始正文也不应传给 Skill。",
                    published_at=datetime.now(timezone.utc),
                ),
            )
            self.assertTrue(inserted)
            repository.save_item_summary(pending_id, summary="待筛选的新消息摘要。")

            client = RecordingClient()
            curator = CurationService(
                repository,
                settings,
                llm_connections=FixedConnection(),  # type: ignore[arg-type]
                client_factory=lambda _config: client,  # type: ignore[arg-type]
            )
            result = curator.curate_available(limit=10)

            self.assertEqual(result.completed, 1)
            feedback = client.calls[0]["recent_feedback"]
            assert isinstance(feedback, list)
            self.assertEqual([item["action"] for item in feedback], ["not_interested", "read"])
            self.assertTrue(
                all(set(item) == {"action", "acted_at", "title", "summary"} for item in feedback)
            )
            self.assertNotIn("绝不能传给筛选 Skill", str(feedback))

    def test_short_posts_are_summarized_without_a_fake_priority_rule(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="RSS", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            item_id, _ = repository.insert_item(
                source_id,
                FeedItem(
                    guid="short",
                    title="一个简短更新",
                    link="https://example.test/short",
                    content="一个足够自足的短帖子。",
                    published_at=datetime.now(timezone.utc),
                ),
            )
            service = SummaryService(repository, settings, llm_connections=FixedConnection(enabled=False))  # type: ignore[arg-type]
            result = service.summarize_pending()
            item = repository.get_item(item_id)
            assert item is not None
            self.assertEqual(result.completed, 1)
            self.assertEqual(item["summary_status"], "complete")
            self.assertIn("一个简短更新", item["summary"])
            # A fallback is version 1 and remains eligible for a future model
            # upgrade, but it must not be regenerated on every worker pass
            # while the model stays disabled.
            self.assertEqual(service.summarize_pending().completed, 0)
