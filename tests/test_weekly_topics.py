from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config import Settings
from app.domain.curation import CurationGroup, EditorialTier
from app.domain.models import FeedItem, SourceDraft, SourceKind
from app.domain.weekly_topics import WeeklyTopicGroup
from app.services.llm_connection import LLMRuntimeConfig
from app.services.weekly_topics import WeeklyTopicService
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


class TopicClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = False

    def complete_json(self, *, system: str, user: dict[str, object]) -> dict[str, object]:
        self.calls.append(user)
        if self.fail:
            raise RuntimeError("temporary topic model failure")
        events = user["events"]
        existing_topics = user["existing_topics"]
        assert isinstance(events, list)
        assert isinstance(existing_topics, list)
        event_ids = [int(event["id"]) for event in events if isinstance(event, dict)]
        reference = "new:1"
        if existing_topics:
            first = existing_topics[0]
            assert isinstance(first, dict)
            reference = f"existing:{int(first['id'])}"
        return {
            "topics": [
                {
                    "ref": reference,
                    "display_name": "MiniMax-M3 发布与评测" if len(event_ids) < 3 else "MiniMax-M3 发布、评测与接入",
                    "event_ids": event_ids,
                }
            ]
        }


def build_settings(root: Path) -> Settings:
    source_dir = root / "sources"
    source_dir.mkdir()
    (source_dir / "user_profile.yml").write_text("identity:\n  description: test\n", encoding="utf-8")
    skill = root / ".agents" / "skills" / "weekly-hot-topics"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# weekly topic test policy\n", encoding="utf-8")
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


class WeeklyTopicTests(unittest.TestCase):
    def test_visible_current_week_events_are_grouped_and_title_can_change_without_new_id(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="RSS", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)

            first_event = self._create_event(
                repository,
                source_id,
                "m3-release",
                "MiniMax-M3 发布",
                now - timedelta(hours=2),
                item_count=2,
            )
            second_event = self._create_event(
                repository,
                source_id,
                "m3-review",
                "MiniMax-M3 实测",
                now - timedelta(hours=1),
            )
            older_event = self._create_event(
                repository,
                source_id,
                "last-week",
                "上周的独立事件",
                now - timedelta(days=3),
            )
            hidden_event = self._create_event(
                repository,
                source_id,
                "hidden",
                "不参与话题的隐藏内容",
                now - timedelta(minutes=30),
                tier=EditorialTier.HIDDEN,
            )
            user_hidden_event = self._create_event(
                repository,
                source_id,
                "user-hidden",
                "用户隐藏的内容",
                now - timedelta(minutes=20),
            )
            repository.mark_event_not_interested(user_hidden_event)

            client = TopicClient()
            service = WeeklyTopicService(
                repository,
                settings,
                llm_connections=FixedConnection(),  # type: ignore[arg-type]
                client_factory=lambda _config: client,  # type: ignore[arg-type]
            )
            first = service.refresh_current_week(now=now, force=True)

            self.assertTrue(first.refreshed)
            self.assertEqual(first.events, 2)
            self.assertEqual(first.topics, 1)
            self.assertEqual(len(client.calls), 1)
            model_events = client.calls[0]["events"]
            assert isinstance(model_events, list)
            self.assertEqual({int(event["id"]) for event in model_events if isinstance(event, dict)}, {first_event, second_event})
            self.assertTrue(
                all(
                    set(event) == {"id", "title", "summary", "content_count", "source_count", "latest_at"}
                    for event in model_events
                    if isinstance(event, dict)
                )
            )
            model_event_ids = {int(event["id"]) for event in model_events if isinstance(event, dict)}
            self.assertNotIn(hidden_event, model_event_ids)
            self.assertNotIn(user_hidden_event, model_event_ids)
            self.assertNotIn(older_event, model_event_ids)

            window = service.current_window(now)
            topics = repository.list_weekly_topics(
                week_start=window.week_start, start=window.start, end=window.end
            )
            self.assertEqual(len(topics), 1)
            topic_id = int(topics[0]["id"])
            self.assertEqual(topics[0]["display_name"], "MiniMax-M3 发布与评测")
            self.assertEqual(topics[0]["content_count"], 3)
            self.assertEqual(topics[0]["event_count"], 2)
            self.assertEqual({int(event["id"]) for event in topics[0]["events"]}, {first_event, second_event})

            third_event = self._create_event(
                repository,
                source_id,
                "m3-access",
                "MiniMax-M3 接入方式",
                now + timedelta(minutes=5),
            )
            second = service.refresh_current_week(now=now + timedelta(minutes=10), force=True)
            self.assertTrue(second.refreshed)
            updated = repository.list_weekly_topics(
                week_start=window.week_start,
                start=window.start,
                end=now + timedelta(minutes=10),
            )
            self.assertEqual(int(updated[0]["id"]), topic_id)
            self.assertEqual(updated[0]["display_name"], "MiniMax-M3 发布、评测与接入")
            self.assertEqual(updated[0]["content_count"], 4)
            self.assertEqual(updated[0]["event_count"], 3)
            self.assertEqual({int(event["id"]) for event in updated[0]["events"]}, {first_event, second_event, third_event})

    def test_failed_refresh_keeps_last_successful_topic_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="RSS", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
            self._create_event(
                repository,
                source_id,
                "first",
                "MiniMax-M3 发布",
                now - timedelta(minutes=1),
                item_count=2,
            )

            client = TopicClient()
            service = WeeklyTopicService(
                repository,
                settings,
                llm_connections=FixedConnection(),  # type: ignore[arg-type]
                client_factory=lambda _config: client,  # type: ignore[arg-type]
            )
            self.assertTrue(service.refresh_current_week(now=now, force=True).refreshed)
            window = service.current_window(now)
            before = repository.list_weekly_topics(
                week_start=window.week_start, start=window.start, end=window.end
            )

            self._create_event(
                repository, source_id, "second", "MiniMax-M3 新评测", now + timedelta(minutes=1)
            )
            client.fail = True
            failed = service.refresh_current_week(now=now + timedelta(minutes=2), force=True)
            after = repository.list_weekly_topics(
                week_start=window.week_start, start=window.start, end=now + timedelta(minutes=2)
            )

            self.assertTrue(failed.failed)
            self.assertEqual([(topic["id"], topic["display_name"]) for topic in after], [(topic["id"], topic["display_name"]) for topic in before])
            self.assertEqual(after[0]["event_count"], 1)

    def test_single_content_is_not_shown_until_topic_coverage_reaches_two(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="RSS", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
            self._create_event(
                repository,
                source_id,
                "first",
                "MiniMax-M3 发布",
                now - timedelta(minutes=1),
            )

            client = TopicClient()
            service = WeeklyTopicService(
                repository,
                settings,
                llm_connections=FixedConnection(),  # type: ignore[arg-type]
                client_factory=lambda _config: client,  # type: ignore[arg-type]
            )
            first = service.refresh_current_week(now=now)
            self.assertTrue(first.refreshed)
            self.assertEqual(first.topics, 0)
            self.assertEqual(len(client.calls), 0)

            window = service.current_window(now)
            self.assertEqual(
                repository.list_weekly_topics(
                    week_start=window.week_start, start=window.start, end=window.end
                ),
                [],
            )
            self.assertTrue(
                service.refresh_current_week(now=now + timedelta(seconds=30)).skipped
            )

            self._create_event(
                repository,
                source_id,
                "review",
                "MiniMax-M3 实测",
                now + timedelta(seconds=30),
            )
            promoted = service.refresh_current_week(now=now + timedelta(minutes=1))
            self.assertTrue(promoted.refreshed)
            self.assertEqual(promoted.topics, 1)
            self.assertEqual(len(client.calls), 1)

            topics = repository.list_weekly_topics(
                week_start=window.week_start,
                start=window.start,
                end=now + timedelta(minutes=1),
            )
            self.assertEqual(len(topics), 1)
            self.assertEqual(topics[0]["content_count"], 2)
            self.assertEqual(topics[0]["event_count"], 2)

    def test_changed_candidates_wait_five_minutes_before_next_topic_model_call(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="RSS", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
            self._create_event(
                repository,
                source_id,
                "release",
                "MiniMax-M3 发布",
                now - timedelta(minutes=1),
                item_count=2,
            )

            client = TopicClient()
            service = WeeklyTopicService(
                repository,
                settings,
                llm_connections=FixedConnection(),  # type: ignore[arg-type]
                client_factory=lambda _config: client,  # type: ignore[arg-type]
            )
            self.assertTrue(service.refresh_current_week(now=now).refreshed)
            self.assertEqual(len(client.calls), 1)

            self._create_event(
                repository,
                source_id,
                "review",
                "MiniMax-M3 实测",
                now + timedelta(seconds=30),
            )
            delayed = service.refresh_current_week(now=now + timedelta(minutes=1))
            self.assertTrue(delayed.skipped)
            self.assertEqual(len(client.calls), 1)

            refreshed = service.refresh_current_week(now=now + timedelta(minutes=5))
            self.assertTrue(refreshed.refreshed)
            self.assertEqual(len(client.calls), 2)

    def test_existing_single_content_topic_is_hidden_from_weekly_hot_topics(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = repository.create_source(
                SourceDraft(name="RSS", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
            event_id = self._create_event(repository, source_id, "single", "单条报道", now)
            service = WeeklyTopicService(repository, settings)
            window = service.current_window(now)
            repository.replace_weekly_topics(
                week_start=window.week_start,
                groups=[
                    WeeklyTopicGroup(
                        ref="new:1",
                        display_name="单条报道话题",
                        event_ids=[event_id],
                    )
                ],
            )

            self.assertEqual(
                repository.list_weekly_topics(
                    week_start=window.week_start, start=window.start, end=window.end
                ),
                [],
            )

    @staticmethod
    def _create_event(
        repository: Repository,
        source_id: int,
        guid_prefix: str,
        title: str,
        published_at: datetime,
        *,
        item_count: int = 1,
        tier: EditorialTier = EditorialTier.IMPORTANT,
    ) -> int:
        item_ids: list[int] = []
        for index in range(item_count):
            item_id, inserted = repository.insert_item(
                source_id,
                FeedItem(
                    guid=f"{guid_prefix}-{index}",
                    title=title,
                    link=f"https://example.test/{guid_prefix}/{index}",
                    content=f"{title} 原始正文，不能传给话题 Skill。",
                    published_at=published_at,
                ),
            )
            assert inserted
            repository.save_item_summary(item_id, summary=f"{title} 的事实摘要。")
            item_ids.append(item_id)
        return repository.apply_curation_groups(
            [
                CurationGroup(
                    item_ids=item_ids,
                    primary_item_id=item_ids[0],
                    tier=tier,
                    reason="测试本周话题",
                    order=1,
                )
            ]
        )[0]
