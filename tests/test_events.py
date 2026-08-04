from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.domain.curation import CurationGroup, EditorialTier
from app.domain.models import FeedItem, SourceDraft, SourceKind
from app.storage.database import Database
from app.storage.repository import Repository


class EventTests(unittest.TestCase):
    def test_latest_items_are_processed_before_historical_backlog(self) -> None:
        directory, repository, source_id = self._repository()
        with directory:
            now = datetime.now(timezone.utc)
            old_id, old_inserted = repository.insert_item(
                source_id,
                FeedItem(
                    guid="old",
                    title="old item",
                    link="https://example.test/old",
                    content="old body",
                    published_at=now - timedelta(days=30),
                ),
            )
            new_id, new_inserted = repository.insert_item(
                source_id,
                FeedItem(
                    guid="new",
                    title="new item",
                    link="https://example.test/new",
                    content="new body",
                    published_at=now,
                ),
            )
            self.assertTrue(old_inserted)
            self.assertTrue(new_inserted)
            self.assertEqual(
                [item["id"] for item in repository.list_items_needing_summary()], [new_id, old_id]
            )

            repository.save_item_summary(old_id, summary="old summary")
            repository.save_item_summary(new_id, summary="new summary")
            self.assertEqual(
                [item["id"] for item in repository.list_items_for_curation()], [new_id, old_id]
            )

    def test_revised_guid_reenters_summary_and_curation(self) -> None:
        directory, repository, source_id = self._repository()
        with directory:
            item_id = self._item(repository, source_id, "revised", "initial title")
            event_id = repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[item_id],
                        primary_item_id=item_id,
                        tier=EditorialTier.BRIEF,
                        reason="initial update",
                        order=1,
                    )
                ]
            )[0]

            revised_id, changed = repository.insert_item(
                source_id,
                FeedItem(
                    guid="revised",
                    title="revised title",
                    link="https://example.test/revised",
                    content="revised body with newly announced availability",
                    published_at=datetime.now(timezone.utc),
                ),
            )
            self.assertEqual(revised_id, item_id)
            self.assertTrue(changed)
            item = repository.get_item(item_id)
            event = repository.get_event(event_id)
            assert item is not None
            assert event is not None
            self.assertEqual(item["title"], "revised title")
            self.assertEqual(item["summary_status"], "pending")
            self.assertEqual(item["summary"], "")
            self.assertEqual(event["curation_status"], "pending")

            repository.save_item_summary(item_id, summary="revised summary")
            self.assertEqual(
                [item["id"] for item in repository.list_items_for_curation()], [item_id]
            )

    def _repository(self) -> tuple[TemporaryDirectory[str], Repository, int]:
        directory = TemporaryDirectory()
        repository = Repository(Database(Path(directory.name) / "test.db"))
        repository.database.initialize()
        source_id = repository.create_source(
            SourceDraft(name="Test", kind=SourceKind.RSS, locator="https://example.test/feed"),
            "https://example.test/feed",
        )
        return directory, repository, source_id

    @staticmethod
    def _item(repository: Repository, source_id: int, guid: str, title: str) -> int:
        item_id, inserted = repository.insert_item(
            source_id,
            FeedItem(
                guid=guid,
                title=title,
                link=f"https://example.test/{guid}",
                content=f"{title} 的原始内容",
                published_at=datetime.now(timezone.utc),
            ),
        )
        assert inserted
        repository.save_item_summary(item_id, summary=f"摘要：{title}")
        return item_id

    def test_paused_source_disappears_from_reader_queries_immediately(self) -> None:
        directory, repository, source_id = self._repository()
        with directory:
            item_id = self._item(repository, source_id, "one", "重要更新")
            event_id = repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[item_id],
                        primary_item_id=item_id,
                        tier=EditorialTier.MUST_READ,
                        reason="影响当前工具使用",
                        order=1,
                    )
                ]
            )[0]
            self.assertEqual(repository.count_events(tier=EditorialTier.MUST_READ, period="all"), 1)
            self.assertEqual(len(repository.list_events(tier=EditorialTier.MUST_READ, period="all")), 1)

            repository.update_source(source_id, {"enabled": 0})
            self.assertEqual(repository.count_events(tier=EditorialTier.MUST_READ, period="all"), 0)
            self.assertEqual(repository.list_events(tier=EditorialTier.MUST_READ, period="all"), [])
            self.assertEqual(repository.get_events_by_ids([event_id]), [])
            self.assertEqual(repository.dashboard_stats()["event_count"], 0)

    def test_user_hidden_event_is_visible_only_in_hidden_tab_and_can_restore(self) -> None:
        directory, repository, source_id = self._repository()
        with directory:
            item_id = self._item(repository, source_id, "two", "一个重要但可隐藏的更新")
            event_id = repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[item_id],
                        primary_item_id=item_id,
                        tier=EditorialTier.IMPORTANT,
                        reason="有明确的新能力",
                        order=1,
                    )
                ]
            )[0]
            repository.mark_event_not_interested(event_id)
            self.assertEqual(repository.list_events(tier=EditorialTier.IMPORTANT, period="all"), [])
            hidden = repository.list_events(tier=EditorialTier.HIDDEN, period="all")
            self.assertEqual([event["id"] for event in hidden], [event_id])
            self.assertTrue(hidden[0]["user_hidden"])

            repository.restore_event(event_id)
            self.assertEqual(len(repository.list_events(tier=EditorialTier.IMPORTANT, period="all")), 1)

    def test_expanded_summary_is_read_until_the_event_receives_a_newer_update(self) -> None:
        directory, repository, source_id = self._repository()
        with directory:
            earlier = datetime.now(timezone.utc) - timedelta(hours=1)
            first_id, inserted = repository.insert_item(
                source_id,
                FeedItem(
                    guid="read-first",
                    title="第一条更新",
                    link="https://example.test/read-first",
                    content="第一条正文",
                    published_at=earlier,
                ),
            )
            self.assertTrue(inserted)
            repository.save_item_summary(first_id, summary="第一条摘要")
            event_id = repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[first_id],
                        primary_item_id=first_id,
                        tier=EditorialTier.IMPORTANT,
                        reason="测试已读状态",
                        order=1,
                    )
                ]
            )[0]

            repository.mark_event_read(event_id)
            read_event = repository.list_events(tier=EditorialTier.IMPORTANT, period="all")[0]
            self.assertTrue(read_event["user_read"])

            later = datetime.now(timezone.utc)
            second_id, inserted = repository.insert_item(
                source_id,
                FeedItem(
                    guid="read-second",
                    title="新增进展",
                    link="https://example.test/read-second",
                    content="新增进展正文",
                    published_at=later,
                ),
            )
            self.assertTrue(inserted)
            repository.save_item_summary(second_id, summary="新增进展摘要")
            repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[first_id, second_id],
                        primary_item_id=second_id,
                        tier=EditorialTier.IMPORTANT,
                        reason="同一事件有新增进展",
                        order=1,
                    )
                ]
            )
            updated_event = repository.list_events(tier=EditorialTier.IMPORTANT, period="all")[0]
            self.assertFalse(updated_event["user_read"])
            self.assertEqual(repository.list_recent_explicit_feedback(), [])

    def test_recent_explicit_feedback_excludes_auto_hidden_and_old_actions(self) -> None:
        directory, repository, source_id = self._repository()
        with directory:
            read_id = self._item(repository, source_id, "feedback-read", "用户已阅读的资讯")
            read_event_id = repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[read_id],
                        primary_item_id=read_id,
                        tier=EditorialTier.IMPORTANT,
                        reason="测试已读反馈",
                        order=1,
                    )
                ]
            )[0]
            repository.mark_event_read(read_event_id)

            negative_id = self._item(repository, source_id, "feedback-negative", "用户不感兴趣的资讯")
            negative_event_id = repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[negative_id],
                        primary_item_id=negative_id,
                        tier=EditorialTier.BRIEF,
                        reason="测试近期负反馈",
                        order=1,
                    )
                ]
            )[0]
            repository.mark_event_read(negative_event_id)
            repository.mark_event_not_interested(negative_event_id)

            auto_hidden_id = self._item(repository, source_id, "auto-hidden", "系统自动隐藏的资讯")
            repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[auto_hidden_id],
                        primary_item_id=auto_hidden_id,
                        tier=EditorialTier.HIDDEN,
                        reason="模型自动隐藏",
                        order=1,
                    )
                ]
            )

            old_id = self._item(repository, source_id, "old-feedback", "过期的已读资讯")
            old_event_id = repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[old_id],
                        primary_item_id=old_id,
                        tier=EditorialTier.IMPORTANT,
                        reason="测试过期反馈",
                        order=1,
                    )
                ]
            )[0]
            repository.mark_event_read(old_event_id)
            old_time = datetime.now(timezone.utc) - timedelta(days=6)
            with repository.database.transaction() as conn:
                conn.execute(
                    "UPDATE feedback SET created_at = ? WHERE event_id = ? AND action = 'read'",
                    (old_time.isoformat(), old_event_id),
                )
                conn.execute(
                    "UPDATE events SET last_seen_at = ? WHERE id = ?",
                    ((old_time - timedelta(minutes=1)).isoformat(), old_event_id),
                )

            feedback = repository.list_recent_explicit_feedback()
            self.assertEqual([item["action"] for item in feedback], ["not_interested", "read"])
            titles = {str(item["title"]) for item in feedback}
            self.assertEqual(titles, {"用户已阅读的资讯", "用户不感兴趣的资讯"})
            self.assertNotIn("系统自动隐藏的资讯", titles)
            self.assertNotIn("过期的已读资讯", titles)
            self.assertTrue(all("content" not in item for item in feedback))

    def test_skill_grouping_merges_multiple_raw_items_into_one_event(self) -> None:
        directory, repository, source_id = self._repository()
        with directory:
            first_id = self._item(repository, source_id, "first", "Flux 3 进入 ComfyUI")
            second_id = self._item(repository, source_id, "second", "ComfyUI 增加 Flux 3 官方节点")
            event_id = repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[first_id, second_id],
                        primary_item_id=second_id,
                        tier=EditorialTier.MUST_READ,
                        reason="顶级模型已可用于核心工作流",
                        order=1,
                    )
                ]
            )[0]
            event = repository.get_event(event_id)
            assert event is not None
            self.assertEqual(event["title"], "ComfyUI 增加 Flux 3 官方节点")
            self.assertEqual(event["editorial_tier"], EditorialTier.MUST_READ.value)
            self.assertEqual(len(event["items"]), 2)
            self.assertEqual(event["visible_item_count"], 2)
            self.assertEqual(event["visible_source_count"], 1)
            listed_event = repository.list_events(tier=EditorialTier.MUST_READ, period="all")[0]
            self.assertEqual(listed_event["visible_item_count"], 2)
            self.assertEqual(listed_event["visible_source_count"], 1)
            self.assertNotIn("importance_score", event)

    def test_curation_order_is_stable_within_a_tier(self) -> None:
        directory, repository, source_id = self._repository()
        with directory:
            first_id = self._item(repository, source_id, "first-order", "第二条")
            second_id = self._item(repository, source_id, "second-order", "第一条")
            repository.apply_curation_groups(
                [
                    CurationGroup(item_ids=[first_id], primary_item_id=first_id, tier=EditorialTier.BRIEF, reason="普通资讯", order=2),
                    CurationGroup(item_ids=[second_id], primary_item_id=second_id, tier=EditorialTier.BRIEF, reason="普通资讯", order=1),
                ]
            )
            events = repository.list_events(tier=EditorialTier.BRIEF, period="all")
            self.assertEqual([event["title"] for event in events], ["第一条", "第二条"])
