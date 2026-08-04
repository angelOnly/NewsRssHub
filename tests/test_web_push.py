from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pywebpush import WebPushException

from app.config import Settings
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

    def test_aggregates_new_items_into_one_homepage_notification_per_interval(self) -> None:
        with TemporaryDirectory() as directory:
            now = [datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)]
            sent: list[dict[str, object]] = []

            def sender(**kwargs: object) -> None:
                sent.append(kwargs)

            repository, service = self._build_service(Path(directory), sender=sender, now=now)
            status = service.save_subscription(sample_subscription())
            self.assertEqual(status.state, "enabled")
            stored = repository.get_connector_credential("web_push_subscription")
            assert stored is not None
            self.assertNotIn("push.example.test", stored["ciphertext"])

            self.assertTrue(service.record_new_items(2))
            self.assertTrue(service.record_new_items(3))
            self.assertEqual(service.deliver_pending().state, "waiting")

            now[0] += timedelta(seconds=61)
            delivery = service.deliver_pending()
            self.assertEqual(delivery.state, "sent")
            self.assertEqual(delivery.pending_count, 5)
            self.assertEqual(len(sent), 1)
            payload = json.loads(str(sent[0]["data"]))
            self.assertEqual(payload["title"], "NewsRSSHub")
            self.assertEqual(payload["body"], "本轮抓取发现 5 条新内容，点此查看")
            self.assertEqual(payload["url"], "/")

            self.assertTrue(service.record_new_items(1))
            self.assertEqual(service.deliver_pending().state, "waiting")
            now[0] += timedelta(minutes=29, seconds=59)
            self.assertEqual(service.deliver_pending().state, "waiting")
            now[0] += timedelta(seconds=1)
            self.assertEqual(service.deliver_pending().state, "sent")
            self.assertEqual(len(sent), 2)

    def test_expired_subscription_is_removed_without_retrying_old_content(self) -> None:
        with TemporaryDirectory() as directory:
            now = [datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)]

            def expired_sender(**_kwargs: object) -> None:
                raise WebPushException("gone", FakePushResponse(410))

            repository, service = self._build_service(Path(directory), sender=expired_sender, now=now)
            service.save_subscription(sample_subscription())
            service.record_new_items(1)
            now[0] += timedelta(seconds=61)

            delivery = service.deliver_pending()
            self.assertEqual(delivery.state, "invalid")
            self.assertIsNone(repository.get_connector_credential("web_push_subscription"))
            self.assertFalse(service.record_new_items(1))

    def test_temporary_failure_keeps_one_pending_notification_for_retry(self) -> None:
        with TemporaryDirectory() as directory:
            now = [datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)]
            calls = 0

            def flaky_sender(**_kwargs: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise WebPushException("unavailable", FakePushResponse(503))

            _repository, service = self._build_service(Path(directory), sender=flaky_sender, now=now)
            service.save_subscription(sample_subscription())
            service.record_new_items(4)
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
