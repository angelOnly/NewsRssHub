from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from requests import HTTPError, Response

from app.config import Settings
from app.domain.models import FeedItem, SourceDraft, SourceKind
from app.plugins.base import PluginRegistry, SourcePlugin
from app.services.collector import Collector
from app.services.connections import ConnectionCatalog
from app.services.x_session import XCredentialStatus
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
        openai_base_url="https://llm.example.test/v1",
        openai_model_name="test-model",
        credential_encryption_key=None,
        timezone="Asia/Shanghai",
        rsshub_base_url="https://rsshub.example.test",
    )


class RecordingPlugin(SourcePlugin):
    """使用基类批量抓取逻辑，验证所有公开来源都收到统一等待回调。"""

    label = "测试来源"

    def __init__(self, kind: SourceKind, failed_source_ids: set[int] | None = None) -> None:
        self.kind = kind
        self.failed_source_ids = failed_source_ids or set()
        self.calls: list[int] = []

    def normalize_locator(self, locator: str) -> str:
        return locator

    def resolve_feed_url(self, locator: str, settings: Settings) -> str:
        return locator

    def fetch(self, source: dict[str, object], settings: Settings) -> list[FeedItem]:
        source_id = int(source["id"])
        self.calls.append(source_id)
        if source_id in self.failed_source_ids:
            raise RuntimeError("模拟抓取失败")
        return [
            FeedItem(
                guid=f"item-{source_id}",
                title=f"来源 {source_id} 的新内容",
                link=f"https://example.test/{source_id}",
                content="测试内容",
                published_at=datetime.now(timezone.utc),
            )
        ]


class RecordingXSession:
    """仅记录 Collector 交给会话服务的 RSSHub 抓取错误。"""

    def __init__(self) -> None:
        self.errors: list[Exception] = []

    def status(self) -> XCredentialStatus:
        return XCredentialStatus("valid", "完整 X Cookie 已验证。", True)

    def record_rsshub_auth_failure(self, error: Exception) -> bool:
        self.errors.append(error)
        return True


class ErrorPlugin(RecordingPlugin):
    """为抓取批次保留同一个原始异常，模拟 RSSHub 的 HTTP 失败。"""

    def __init__(self, kind: SourceKind, error: Exception) -> None:
        super().__init__(kind)
        self.error = error

    def fetch(self, source: dict[str, object], settings: Settings) -> list[FeedItem]:
        self.calls.append(int(source["id"]))
        raise self.error


class FetchPolicyRepositoryTests(unittest.TestCase):
    def test_new_schema_has_global_policy_and_default_interval(self) -> None:
        with TemporaryDirectory() as directory:
            database = Database(Path(directory) / "data" / "test.db")
            database.initialize()
            repository = Repository(database)

            with database.read() as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(sources)")}
                setting = connection.execute(
                    "SELECT value FROM app_settings WHERE key = ?",
                    (Repository.FETCH_INTERVAL_SETTING,),
                ).fetchone()

            self.assertTrue({"next_fetch_at", "last_new_item_count"} <= columns)
            self.assertIsNotNone(setting)
            self.assertEqual(setting[0], "60")
            self.assertEqual(repository.get_fetch_policy().interval_minutes, 60)

    def test_saving_policy_replans_enabled_sources_and_next_run_adds_interval(self) -> None:
        with TemporaryDirectory() as directory:
            database = Database(Path(directory) / "data" / "test.db")
            database.initialize()
            repository = Repository(database)
            active_ids = [
                repository.create_source(
                    SourceDraft(name=name, kind=SourceKind.RSS, locator=f"https://example.test/{name}"),
                    f"https://example.test/{name}",
                )
                for name in ("one", "two")
            ]
            paused_id = repository.create_source(
                SourceDraft(
                    name="paused",
                    kind=SourceKind.RSS,
                    locator="https://example.test/paused",
                    enabled=False,
                ),
                "https://example.test/paused",
            )
            now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

            policy, rescheduled = repository.save_fetch_policy(
                30,
                now=now,
                jitter_provider=lambda _minimum, _maximum: 60,
            )

            self.assertEqual(policy.interval_minutes, 30)
            self.assertEqual(rescheduled, 2)
            for source_id in active_ids:
                source = repository.get_source(source_id)
                assert source is not None
                self.assertEqual(source["next_fetch_at"], (now + timedelta(minutes=1)).isoformat())
            paused = repository.get_source(paused_id)
            assert paused is not None
            self.assertIsNone(paused["next_fetch_at"])
            self.assertEqual(repository.due_sources(now + timedelta(seconds=59)), [])
            self.assertEqual(
                {int(source["id"]) for source in repository.due_sources(now + timedelta(minutes=1))},
                set(active_ids),
            )

            next_fetch_at = repository.schedule_next_fetch(
                active_ids[0],
                policy,
                now=now,
                jitter_provider=lambda _minimum, _maximum: 300,
            )
            self.assertEqual(next_fetch_at, (now + timedelta(minutes=35)).isoformat())


class CollectorSchedulingTests(unittest.TestCase):
    def test_first_pass_only_creates_initial_schedule(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            database = Database(settings.database_path)
            database.initialize()
            repository = Repository(database)
            source_ids = [
                repository.create_source(
                    SourceDraft(name=name, kind=SourceKind.RSS, locator=f"https://example.test/{name}"),
                    f"https://example.test/{name}",
                )
                for name in ("one", "two")
            ]
            plugin = RecordingPlugin(SourceKind.RSS)

            summary = Collector(
                repository,
                PluginRegistry([plugin]),
                settings,
                ConnectionCatalog(rsshub_base_url=settings.rsshub_base_url),
                sleeper=lambda _seconds: None,
                delay_provider=lambda: 2.0,
            ).collect_due_sources()

            self.assertEqual(summary.sources_scheduled, 2)
            self.assertEqual(summary.sources_checked, 0)
            self.assertEqual(plugin.calls, [])
            for source_id in source_ids:
                source = repository.get_source(source_id)
                assert source is not None
                self.assertIsNotNone(source["next_fetch_at"])

    def test_collector_spaces_all_sources_and_reschedules_success_and_failure(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            database = Database(settings.database_path)
            database.initialize()
            repository = Repository(database)
            rss_one = repository.create_source(
                SourceDraft(name="RSS one", kind=SourceKind.RSS, locator="https://example.test/rss-one"),
                "https://example.test/rss-one",
            )
            reddit = repository.create_source(
                SourceDraft(name="Reddit", kind=SourceKind.REDDIT, locator="r/example"),
                "https://www.reddit.com/r/example/.rss",
            )
            rss_two = repository.create_source(
                SourceDraft(name="RSS two", kind=SourceKind.RSS, locator="https://example.test/rss-two"),
                "https://example.test/rss-two",
            )
            due_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0).isoformat()
            for source_id in (rss_one, reddit, rss_two):
                repository.update_source(source_id, {"next_fetch_at": due_at})

            waits: list[float] = []
            rss_plugin = RecordingPlugin(SourceKind.RSS)
            reddit_plugin = RecordingPlugin(SourceKind.REDDIT, {reddit})
            started_at = datetime.now(timezone.utc)
            summary = Collector(
                repository,
                PluginRegistry([rss_plugin, reddit_plugin]),
                settings,
                ConnectionCatalog(rsshub_base_url=settings.rsshub_base_url),
                sleeper=waits.append,
                delay_provider=lambda: 2.5,
            ).collect_due_sources()
            finished_at = datetime.now(timezone.utc)

            self.assertEqual(summary.sources_checked, 3)
            self.assertEqual(summary.sources_failed, 1)
            self.assertEqual(summary.new_items, 2)
            self.assertEqual(waits, [2.5, 2.5])

            successful = repository.get_source(rss_one)
            failed = repository.get_source(reddit)
            assert successful is not None and failed is not None
            self.assertEqual(successful["health_status"], "healthy")
            self.assertEqual(successful["last_new_item_count"], 1)
            self.assertEqual(failed["health_status"], "error")
            self.assertEqual(failed["last_new_item_count"], 0)
            lower_bound = started_at.replace(microsecond=0) + timedelta(minutes=61)
            upper_bound = finished_at + timedelta(minutes=65)
            for source in (successful, failed):
                next_fetch_at = datetime.fromisoformat(str(source["next_fetch_at"]))
                self.assertGreaterEqual(next_fetch_at, lower_bound)
                self.assertLessEqual(next_fetch_at, upper_bound)

    def test_x_rsshub_failure_is_forwarded_to_the_session_health_check(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            database = Database(settings.database_path)
            database.initialize()
            repository = Repository(database)
            source_id = repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="OpenAI"),
                "https://rsshub.example.test/twitter/user/OpenAI",
            )
            due_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0).isoformat()
            repository.update_source(source_id, {"next_fetch_at": due_at})
            session = RecordingXSession()
            response = Response()
            response.status_code = 503
            response._content = b"Twitter API error: 403"
            expected_error = HTTPError("503 Server Error", response=response)
            plugin = ErrorPlugin(SourceKind.X_RSSHUB, expected_error)

            # 让插件返回带有 RSSHub 鉴权特征的原始异常，验证 Collector 不会丢失它。
            summary = Collector(
                repository,
                PluginRegistry([plugin]),
                settings,
                ConnectionCatalog(session, settings.rsshub_base_url),
                sleeper=lambda _seconds: None,
                delay_provider=lambda: 0.0,
            ).collect_due_sources()

            self.assertEqual(summary.sources_failed, 1)
            self.assertEqual(session.errors, [expected_error])


if __name__ == "__main__":
    unittest.main()
