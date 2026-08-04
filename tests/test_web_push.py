from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from py_vapid import Vapid
from pywebpush import WebPushException

from app.config import Settings
from app.domain.curation import CurationGroup, EditorialTier
from app.domain.models import FeedItem, SourceDraft, SourceKind
from app.runtime import build_services
from app.services.web_push import WebPushConfigurationError, WebPushService
from app.storage.database import Database
from app.storage.repository import Repository
from app.web import app


def build_settings(
    root: Path,
    *,
    credential_key: str | None = None,
    web_push_subject: str = "https://news.example.test",
) -> Settings:
    source_dir = root / "sources"
    source_dir.mkdir()
    skill = root / ".agents" / "skills" / "curate-personal-news"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# policy\n", encoding="utf-8")
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
        credential_encryption_key=credential_key,
        timezone="Asia/Shanghai",
        web_push_subject=web_push_subject,
    )


def sample_subscription() -> dict[str, object]:
    return {
        "endpoint": "https://push.example.test/subscriptions/one",
        "keys": {"p256dh": "A" * 87, "auth": "B" * 22},
    }


class FakePushResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = ""


class WebPushServiceTests(unittest.TestCase):
    def _build_service(
        self,
        root: Path,
        *,
        sender: object,
        now: list[datetime],
    ) -> tuple[Repository, WebPushService]:
        settings = build_settings(root, credential_key=Fernet.generate_key().decode("ascii"))
        repository = Repository(Database(settings.database_path))
        repository.database.initialize()
        repository.save_fetch_policy(30, now=now[0], jitter_provider=lambda _min, _max: 60)
        service = WebPushService(
            repository,
            settings,
            sender=sender,  # type: ignore[arg-type]
            now_provider=lambda: now[0],
        )
        return repository, service

    @staticmethod
    def _create_event(
        repository: Repository,
        source_id: int,
        *,
        guid: str,
        published_at: datetime,
        tier: EditorialTier = EditorialTier.MUST_READ,
    ) -> int:
        item_id, inserted = repository.insert_item(
            source_id,
            FeedItem(
                guid=guid,
                title=f"新闻 {guid}",
                link=f"https://news.example.test/{guid}",
                content=f"新闻 {guid} 的正文",
                published_at=published_at,
            ),
        )
        assert inserted
        repository.save_item_summary(item_id, summary=f"新闻 {guid} 的摘要")
        return repository.apply_curation_groups(
            [
                CurationGroup(
                    item_ids=[item_id],
                    primary_item_id=item_id,
                    tier=tier,
                    reason="用于验证手机通知的新闻计数",
                    order=1,
                )
            ]
        )[0]

    def test_notifies_with_recent_unread_event_count(self) -> None:
        with TemporaryDirectory() as directory:
            now = [datetime.now(timezone.utc).replace(microsecond=0)]
            sent: list[dict[str, object]] = []

            def sender(**kwargs: object) -> None:
                sent.append(kwargs)

            repository, service = self._build_service(Path(directory), sender=sender, now=now)
            status = service.save_subscription(sample_subscription())
            self.assertEqual(status.state, "enabled")
            stored = repository.get_connector_credential("web_push_subscription")
            assert stored is not None
            self.assertNotIn("push.example.test", stored["ciphertext"])

            source_id = repository.create_source(
                SourceDraft(name="测试 RSS", kind=SourceKind.RSS, locator="https://news.example.test/feed"),
                "https://news.example.test/feed",
            )
            self._create_event(repository, source_id, guid="one", published_at=now[0])
            self._create_event(repository, source_id, guid="two", published_at=now[0])

            # 原始抓取新增量即使很大，通知也以用户实际可阅读的合并新闻数为准。
            self.assertTrue(service.record_ready_items(2))
            self.assertTrue(service.record_ready_items(300))
            self.assertEqual(service.deliver_pending().state, "waiting")

            now[0] += timedelta(seconds=61)
            delivery = service.deliver_pending()
            self.assertEqual(delivery.state, "sent")
            self.assertEqual(delivery.pending_count, 2)
            self.assertEqual(len(sent), 1)
            payload = json.loads(str(sent[0]["data"]))
            self.assertEqual(payload["title"], "NewsRSSHub")
            self.assertEqual(payload["body"], "最近 2 小时有 2 条未读新内容，点此查看")
            self.assertEqual(payload["url"], "/")

    def test_uses_saved_notification_window(self) -> None:
        with TemporaryDirectory() as directory:
            now = [datetime.now(timezone.utc).replace(microsecond=0)]
            sent: list[dict[str, object]] = []
            repository, service = self._build_service(
                Path(directory),
                sender=lambda **kwargs: sent.append(kwargs),
                now=now,
            )
            service.save_subscription(sample_subscription())
            source_id = repository.create_source(
                SourceDraft(name="测试 RSS", kind=SourceKind.RSS, locator="https://news.example.test/feed"),
                "https://news.example.test/feed",
            )
            self._create_event(
                repository,
                source_id,
                guid="three-hours-old",
                published_at=now[0] - timedelta(hours=3),
            )

            self.assertEqual(repository.get_web_push_window_hours(), 2)
            self.assertTrue(service.record_ready_items(1))
            now[0] += timedelta(seconds=61)
            self.assertEqual(service.deliver_pending().state, "empty")
            self.assertEqual(sent, [])

            self.assertEqual(repository.save_web_push_window_hours(4), 4)
            self.assertTrue(service.record_ready_items(1))
            now[0] += timedelta(seconds=61)
            self.assertEqual(service.deliver_pending().state, "sent")
            payload = json.loads(str(sent[0]["data"]))
            self.assertEqual(payload["body"], "最近 4 小时有 1 条未读新内容，点此查看")

    def test_skips_push_when_recent_events_are_read_or_expired(self) -> None:
        with TemporaryDirectory() as directory:
            now = [datetime.now(timezone.utc).replace(microsecond=0)]
            sent: list[dict[str, object]] = []
            repository, service = self._build_service(
                Path(directory),
                sender=lambda **kwargs: sent.append(kwargs),
                now=now,
            )
            service.save_subscription(sample_subscription())
            source_id = repository.create_source(
                SourceDraft(name="测试 RSS", kind=SourceKind.RSS, locator="https://news.example.test/feed"),
                "https://news.example.test/feed",
            )
            read_event_id = self._create_event(
                repository,
                source_id,
                guid="read",
                published_at=now[0] - timedelta(hours=1),
            )
            self._create_event(
                repository,
                source_id,
                guid="old",
                published_at=now[0] - timedelta(hours=7),
            )
            self._create_event(
                repository,
                source_id,
                guid="hidden",
                published_at=now[0] - timedelta(hours=1),
                tier=EditorialTier.HIDDEN,
            )
            repository.mark_event_read(read_event_id)

            self.assertTrue(service.record_ready_items(200))
            now[0] += timedelta(seconds=61)
            self.assertEqual(service.deliver_pending().state, "empty")
            self.assertEqual(sent, [])
            self.assertEqual(service.deliver_pending().state, "idle")

    def test_legacy_raw_item_counter_is_not_delivered_after_upgrade(self) -> None:
        with TemporaryDirectory() as directory:
            now = [datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)]
            repository, service = self._build_service(
                Path(directory),
                sender=lambda **_kwargs: self.fail("不应投递旧版本累计的原始条目数"),
                now=now,
            )
            service.save_subscription(sample_subscription())
            repository.save_app_setting(
                "web_push_state",
                json.dumps(
                    {
                        "pending_count": 200,
                        "due_at": (now[0] - timedelta(minutes=1)).isoformat(),
                        "last_sent_at": (now[0] - timedelta(minutes=30)).isoformat(),
                    }
                ),
            )

            self.assertEqual(service.deliver_pending().state, "idle")

    def test_expired_subscription_is_removed_without_retrying_old_content(self) -> None:
        with TemporaryDirectory() as directory:
            now = [datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)]

            def expired_sender(**_kwargs: object) -> None:
                raise WebPushException("gone", FakePushResponse(410))

            repository, service = self._build_service(Path(directory), sender=expired_sender, now=now)
            service.save_subscription(sample_subscription())
            source_id = repository.create_source(
                SourceDraft(name="测试 RSS", kind=SourceKind.RSS, locator="https://news.example.test/feed"),
                "https://news.example.test/feed",
            )
            self._create_event(repository, source_id, guid="expired", published_at=now[0])
            service.record_ready_items(1)
            now[0] += timedelta(seconds=61)

            delivery = service.deliver_pending()
            self.assertEqual(delivery.state, "invalid")
            self.assertIsNone(repository.get_connector_credential("web_push_subscription"))
            self.assertFalse(service.record_ready_items(1))

    def test_temporary_failure_keeps_one_pending_notification_for_retry(self) -> None:
        with TemporaryDirectory() as directory:
            now = [datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)]
            calls = 0

            def flaky_sender(**_kwargs: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise WebPushException("unavailable", FakePushResponse(503))

            repository, service = self._build_service(Path(directory), sender=flaky_sender, now=now)
            service.save_subscription(sample_subscription())
            source_id = repository.create_source(
                SourceDraft(name="测试 RSS", kind=SourceKind.RSS, locator="https://news.example.test/feed"),
                "https://news.example.test/feed",
            )
            self._create_event(repository, source_id, guid="retry", published_at=now[0])
            service.record_ready_items(4)
            now[0] += timedelta(seconds=61)
            self.assertEqual(service.deliver_pending().state, "retry")
            self.assertEqual(service.deliver_pending().state, "waiting")
            now[0] += timedelta(seconds=60)
            self.assertEqual(service.deliver_pending().state, "sent")
            self.assertEqual(calls, 2)

    def test_invalid_vapid_subject_is_reported_before_saving_subscription(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = [datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)]
            settings = build_settings(
                root,
                credential_key=Fernet.generate_key().decode("ascii"),
                web_push_subject="http://insecure.example.test",
            )
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            service = WebPushService(
                repository,
                settings,
                sender=lambda **_kwargs: None,
                now_provider=lambda: now[0],
            )

            with self.assertRaisesRegex(WebPushConfigurationError, "发件人标识无效"):
                service.public_config()
            with self.assertRaisesRegex(WebPushConfigurationError, "发件人标识无效"):
                service.save_subscription(sample_subscription())
            self.assertIsNone(repository.get_connector_credential("web_push_subscription"))

    def test_vapid_subject_normalizes_https_port_for_py_vapid(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(
                Path(directory),
                credential_key=Fernet.generate_key().decode("ascii"),
                web_push_subject="https://news.example.test:18443",
            )
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            service = WebPushService(repository, settings, sender=lambda **_kwargs: None)

            subject = service._vapid_subject()
            self.assertEqual(subject, "https://news.example.test")
            vapid = Vapid()
            vapid.generate_keys()
            vapid.sign({"sub": subject, "aud": "https://push.example.test"})


class WebPushRouteTests(unittest.TestCase):
    def test_pwa_assets_and_push_subscription_routes(self) -> None:
        with TemporaryDirectory() as directory:
            services = build_services(
                build_settings(Path(directory), credential_key=Fernet.generate_key().decode("ascii"))
            )
            sent: list[dict[str, object]] = []
            services.web_push._sender = lambda **kwargs: sent.append(kwargs)
            app.state.services = services
            try:
                with TestClient(app) as client:
                    manifest = client.get("/manifest.webmanifest")
                    self.assertEqual(manifest.status_code, 200)
                    self.assertIn("application/manifest+json", manifest.headers["content-type"])
                    self.assertEqual(manifest.json()["display"], "standalone")

                    worker = client.get("/sw.js")
                    self.assertEqual(worker.status_code, 200)
                    self.assertIn("no-store", worker.headers["cache-control"])
                    self.assertIn("notificationclick", worker.text)

                    settings_page = client.get("/settings")
                    self.assertIn("data-push-controls", settings_page.text)
                    self.assertIn("data-push-state", settings_page.text)
                    self.assertIn("/static/push-client.js", settings_page.text)

                    config = client.get("/api/push/config")
                    self.assertEqual(config.status_code, 200)
                    self.assertTrue(config.json()["available"])
                    self.assertIn("public_key", config.json())

                    blocked = client.post("/api/push/subscription", json=sample_subscription())
                    self.assertEqual(blocked.status_code, 403)

                    subscribed = client.post(
                        "/api/push/subscription",
                        json=sample_subscription(),
                        headers={"X-NewsRSSHub-Push": "1"},
                    )
                    self.assertEqual(subscribed.status_code, 200)
                    self.assertEqual(subscribed.json()["status"]["state"], "enabled")

                    tested = client.post(
                        "/api/push/test",
                        json={},
                        headers={"X-NewsRSSHub-Push": "1"},
                    )
                    self.assertEqual(tested.status_code, 200)
                    self.assertEqual(len(sent), 1)

                    removed = client.delete(
                        "/api/push/subscription",
                        headers={"X-NewsRSSHub-Push": "1"},
                    )
                    self.assertEqual(removed.status_code, 204)
                    self.assertFalse(services.web_push.status().configured)
            finally:
                delattr(app.state, "services")


if __name__ == "__main__":
    unittest.main()
