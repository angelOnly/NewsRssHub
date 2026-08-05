from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from app.config import Settings
from app.domain.curation import CurationGroup, EditorialTier
from app.domain.models import FeedItem, SourceDraft, SourceKind
from app.domain.weekly_topics import DailyTopicOutput
from app.services.llm_connection import LLMRuntimeConfig
from app.services.weekly_topics import DailyTopicService
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
    """模拟严格的新增事件输入与 existing/new 输出协议。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.options: list[dict[str, object]] = []
        self.fail = False

    def complete_json(
        self, *, system: str, user: dict[str, object], **options: object
    ) -> dict[str, object]:
        self.calls.append(user)
        self.options.append(options)
        if self.fail:
            raise RuntimeError("temporary topic model failure")
        events = user["new_events"]
        existing_topics = user["existing_topics"]
        assert isinstance(events, list)
        assert isinstance(existing_topics, list)
        event_ids = [int(event["id"]) for event in events if isinstance(event, dict)]
        if existing_topics:
            first = existing_topics[0]
            assert isinstance(first, dict)
            return {
                "topics": [
                    {
                        "ref": f"existing:{int(first['id'])}",
                        "event_ids": event_ids,
                    }
                ]
            }
        return {
            "topics": [
                {
                    "ref": "new:1",
                    "display_name": "MiniMax-M3 发布与评测",
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
    (skill / "SKILL.md").write_text("# daily topic test policy\n", encoding="utf-8")
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


class DailyTopicTests(unittest.TestCase):
    def test_refresh_interval_defaults_to_thirty_minutes_and_migrates_legacy_setting(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()

            self.assertEqual(repository.get_daily_topic_refresh_interval_minutes(), 30)
            repository.save_app_setting("weekly_topic_refresh_interval_minutes", "45")
            self.assertEqual(repository.get_daily_topic_refresh_interval_minutes(), 45)
            self.assertEqual(repository.save_daily_topic_refresh_interval_minutes("1"), 5)
            self.assertEqual(repository.get_daily_topic_refresh_interval_minutes(), 5)
            self.assertEqual(repository.save_daily_topic_refresh_interval_minutes("2000"), 1440)

    def test_daily_refresh_only_assigns_new_visible_events_and_keeps_existing_names(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = self._create_source(repository)
            now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)

            first_event = self._create_event(
                repository, source_id, "m3-release", "MiniMax-M3 发布", now - timedelta(hours=2), item_count=2
            )
            second_event = self._create_event(
                repository, source_id, "m3-review", "MiniMax-M3 实测", now - timedelta(hours=1)
            )
            older_event = self._create_event(
                repository, source_id, "old", "昨天的独立事件", now - timedelta(days=2)
            )
            hidden_event = self._create_event(
                repository,
                source_id,
                "hidden",
                "系统隐藏内容",
                now - timedelta(minutes=30),
                tier=EditorialTier.HIDDEN,
            )
            user_hidden_event = self._create_event(
                repository, source_id, "user-hidden", "用户隐藏内容", now - timedelta(minutes=20)
            )
            repository.mark_event_not_interested(user_hidden_event)

            client = TopicClient()
            service = self._service(repository, settings, client)
            first = service.refresh_current_day(now=now, force=True)

            self.assertTrue(first.refreshed)
            self.assertEqual(first.events, 2)
            self.assertEqual(first.topics, 1)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(
                client.options[0],
                {
                    "stream": True,
                    "read_timeout": None,
                    "extra_body": {"thinking": {"type": "disabled"}},
                },
            )
            model_events = client.calls[0]["new_events"]
            assert isinstance(model_events, list)
            self.assertEqual(
                {int(event["id"]) for event in model_events if isinstance(event, dict)},
                {first_event, second_event},
            )
            self.assertTrue(
                all(set(event) == {"id", "title", "summary"} for event in model_events if isinstance(event, dict))
            )
            self.assertNotIn(older_event, {int(event["id"]) for event in model_events if isinstance(event, dict)})
            self.assertNotIn(hidden_event, {int(event["id"]) for event in model_events if isinstance(event, dict)})
            self.assertNotIn(user_hidden_event, {int(event["id"]) for event in model_events if isinstance(event, dict)})

            window = service.current_window(now)
            topics = repository.list_daily_topics(
                topic_date=window.topic_date, start=window.start, end=window.end
            )
            self.assertEqual(len(topics), 1)
            topic_id = int(topics[0]["id"])
            self.assertEqual(topics[0]["display_name"], "MiniMax-M3 发布与评测")
            self.assertEqual(topics[0]["content_count"], 3)
            self.assertEqual(topics[0]["event_count"], 2)
            self.assertEqual(topics[0]["description"], "MiniMax-M3 发布 的事实摘要。")

            third_event = self._create_event(
                repository, source_id, "m3-access", "MiniMax-M3 接入方式", now + timedelta(minutes=5)
            )
            second = service.refresh_current_day(now=now + timedelta(minutes=10), force=True)
            self.assertTrue(second.refreshed)
            self.assertEqual(second.events, 1)
            self.assertEqual(len(client.calls), 2)
            existing_topics = client.calls[1]["existing_topics"]
            assert isinstance(existing_topics, list)
            self.assertEqual(existing_topics, [{"id": topic_id, "display_name": "MiniMax-M3 发布与评测"}])

            updated = repository.list_daily_topics(
                topic_date=window.topic_date,
                start=window.start,
                end=now + timedelta(minutes=10),
            )
            self.assertEqual(int(updated[0]["id"]), topic_id)
            self.assertEqual(updated[0]["display_name"], "MiniMax-M3 发布与评测")
            self.assertEqual(updated[0]["content_count"], 4)
            self.assertEqual(updated[0]["event_count"], 3)
            self.assertEqual({int(event["id"]) for event in updated[0]["events"]}, {first_event, second_event, third_event})
            self.assertTrue(service.refresh_current_day(now=now + timedelta(minutes=20)).skipped)
            self.assertEqual(len(client.calls), 2)

    def test_single_content_is_assigned_before_becoming_visible_hotspot(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = self._create_source(repository)
            now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
            first_event = self._create_event(
                repository, source_id, "first", "MiniMax-M3 发布", now - timedelta(minutes=1)
            )

            client = TopicClient()
            service = self._service(repository, settings, client)
            first = service.refresh_current_day(now=now)
            self.assertTrue(first.refreshed)
            self.assertEqual(len(client.calls), 1)

            window = service.current_window(now)
            self.assertEqual(len(repository.list_daily_topic_state(window.topic_date)), 1)
            self.assertEqual(
                repository.list_daily_topics(
                    topic_date=window.topic_date, start=window.start, end=window.end
                ),
                [],
            )

            second_event = self._create_event(
                repository, source_id, "review", "MiniMax-M3 实测", now + timedelta(seconds=30)
            )
            second = service.refresh_current_day(now=now + timedelta(minutes=6))
            self.assertTrue(second.refreshed)
            topics = repository.list_daily_topics(
                topic_date=window.topic_date, start=window.start, end=now + timedelta(minutes=6)
            )
            self.assertEqual(len(topics), 1)
            self.assertEqual(topics[0]["content_count"], 2)
            self.assertEqual(topics[0]["event_count"], 2)
            labels = repository.list_daily_topic_names_for_events(
                topic_date=window.topic_date,
                start=window.start,
                end=now + timedelta(minutes=6),
                event_ids=[first_event, second_event],
            )
            self.assertEqual(
                labels,
                {
                    first_event: "MiniMax-M3 发布与评测",
                    second_event: "MiniMax-M3 发布与评测",
                },
            )

    def test_failed_increment_keeps_existing_assignments_and_retries_only_unassigned_events(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = self._create_source(repository)
            now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
            first_event = self._create_event(
                repository, source_id, "first", "MiniMax-M3 发布", now - timedelta(minutes=1), item_count=2
            )

            client = TopicClient()
            service = self._service(repository, settings, client)
            self.assertTrue(service.refresh_current_day(now=now, force=True).refreshed)
            window = service.current_window(now)
            topic_id = int(repository.list_daily_topic_state(window.topic_date)[0]["id"])

            second_event = self._create_event(
                repository, source_id, "second", "MiniMax-M3 新评测", now + timedelta(minutes=1)
            )
            client.fail = True
            failed = service.refresh_current_day(now=now + timedelta(minutes=2), force=True)
            self.assertTrue(failed.failed)
            topics_after_failure = repository.list_daily_topics(
                topic_date=window.topic_date, start=window.start, end=now + timedelta(minutes=2)
            )
            self.assertEqual(int(topics_after_failure[0]["id"]), topic_id)
            self.assertEqual({int(event["id"]) for event in topics_after_failure[0]["events"]}, {first_event})

            client.fail = False
            retried = service.refresh_current_day(now=now + timedelta(minutes=8))
            self.assertTrue(retried.refreshed)
            topics_after_retry = repository.list_daily_topics(
                topic_date=window.topic_date, start=window.start, end=now + timedelta(minutes=8)
            )
            self.assertEqual(
                {int(event["id"]) for event in topics_after_retry[0]["events"]},
                {first_event, second_event},
            )

    def test_daily_window_is_local_natural_day_not_rolling_twenty_four_hours(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = self._create_source(repository)
            # 17:00 UTC 是上海时间 8 月 5 日 01:00；前一天 23:30 的内容不能进入今日。
            now = datetime(2026, 8, 4, 17, tzinfo=timezone.utc)
            yesterday_event = self._create_event(
                repository, source_id, "yesterday", "昨天深夜的内容", datetime(2026, 8, 4, 15, 30, tzinfo=timezone.utc)
            )
            today_event = self._create_event(
                repository, source_id, "today", "今日凌晨的内容", datetime(2026, 8, 4, 16, 10, tzinfo=timezone.utc)
            )
            client = TopicClient()
            service = self._service(repository, settings, client)

            result = service.refresh_current_day(now=now, force=True)
            self.assertTrue(result.refreshed)
            model_events = client.calls[0]["new_events"]
            assert isinstance(model_events, list)
            event_ids = {int(event["id"]) for event in model_events if isinstance(event, dict)}
            self.assertEqual(event_ids, {today_event})
            self.assertNotIn(yesterday_event, event_ids)

    def test_compact_input_and_strict_output_contract(self) -> None:
        events = DailyTopicService._skill_events(
            [
                {
                    "id": 7,
                    "title": "题" * 31,
                    "summary": "要" * 101,
                    "content_count": 99,
                    "source_count": 8,
                    "content": "这是不能传给话题模型的原始正文。",
                }
            ]
        )
        self.assertEqual(events, [{"id": 7, "title": "题" * 29 + "…", "summary": "要" * 99 + "…"}])
        existing = DailyTopicService._skill_existing_topics(
            [{"id": 42, "display_name": "MiniMax-M3 发布与评测", "event_ids": [1, 2]}]
        )
        self.assertEqual(existing, [{"id": 42, "display_name": "MiniMax-M3 发布与评测"}])

        with self.assertRaises(ValidationError):
            DailyTopicOutput.model_validate(
                {"topics": [{"ref": "existing:42", "display_name": "不允许改名", "event_ids": [1]}]}
            )
        with self.assertRaises(ValidationError):
            DailyTopicOutput.model_validate(
                {"topics": [{"ref": "new:1", "event_ids": [1]}]}
            )

    def test_batch_limit_leaves_remaining_events_for_the_next_increment(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            source_id = self._create_source(repository)
            now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
            for index in range(3):
                self._create_event(
                    repository,
                    source_id,
                    f"event-{index}",
                    f"MiniMax-M3 更新 {index}",
                    now - timedelta(minutes=3 - index),
                )

            client = TopicClient()
            service = self._service(repository, settings, client)
            service.MAX_EVENTS_PER_REQUEST = 2
            first = service.refresh_current_day(now=now, force=True)
            second = service.refresh_current_day(now=now + timedelta(minutes=1), force=True)

            self.assertEqual(first.events, 2)
            self.assertEqual(second.events, 1)
            self.assertEqual([len(call["new_events"]) for call in client.calls], [2, 1])

    @staticmethod
    def _create_source(repository: Repository) -> int:
        return repository.create_source(
            SourceDraft(name="RSS", kind=SourceKind.RSS, locator="https://example.test/feed"),
            "https://example.test/feed",
        )

    @staticmethod
    def _service(repository: Repository, settings: Settings, client: TopicClient) -> DailyTopicService:
        return DailyTopicService(
            repository,
            settings,
            llm_connections=FixedConnection(),  # type: ignore[arg-type]
            client_factory=lambda _config: client,  # type: ignore[arg-type]
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
                    reason="测试今日话题",
                    order=1,
                )
            ]
        )[0]
