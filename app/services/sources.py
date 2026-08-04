from __future__ import annotations

from dataclasses import replace
import re
from typing import Any, Sequence

import yaml

from app.config import Settings
from app.domain.models import SourceDraft, SourceKind, ValidationResult
from app.plugins.base import PluginRegistry
from app.services.connections import ConnectionCatalog
from app.services.rsshub_runtime import RssHubRuntimeFiles
from app.storage.repository import Repository


class SourceService:
    def __init__(
        self,
        repository: Repository,
        registry: PluginRegistry,
        settings: Settings,
        connections: ConnectionCatalog | None = None,
        runtime_files: RssHubRuntimeFiles | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.settings = settings
        self.connections = connections or ConnectionCatalog(rsshub_base_url=settings.rsshub_base_url)
        self.runtime_files = runtime_files or RssHubRuntimeFiles(settings)

    def prepare_draft(self, draft: SourceDraft) -> tuple[SourceDraft, str]:
        """Turn user input into one normalized, durable source draft."""

        plugin = self.registry.get(draft.kind)
        locator, feed_url = plugin.prepare_source(draft.locator, self.settings)
        # 历史字段仍写入数据库以兼容旧备份，但调度只读取全局策略。
        return replace(
            draft,
            locator=locator,
            description=draft.description.strip()[:300],
            poll_interval_minutes=self.repository.get_fetch_policy().interval_minutes,
        ), feed_url

    def add_source(
        self,
        draft: SourceDraft,
        validate: bool = True,
        *,
        require_connection: bool = True,
    ) -> tuple[dict[str, Any], ValidationResult | None]:
        # Enforce the same platform-first rule used by the web setup flow.  It
        # runs before inserting a source, so an unconfigured X account never
        # becomes a broken row in the database.
        if require_connection:
            self.connections.ensure_source_ready(draft.kind)
        normalized, feed_url = self.prepare_draft(draft)
        source_id = self.repository.create_source(normalized, feed_url)
        # 新增通用 RSS 后先更新白名单，随后的 RSSHub 连通性测试才能访问它。
        self.sync_rsshub_runtime()
        source = self.repository.get_source(source_id)
        assert source is not None

        result: ValidationResult | None = None
        if validate:
            result = self.validate_source(source_id)
            source = self.repository.get_source(source_id)
            assert source is not None
        if source["enabled"]:
            self.repository.schedule_initial_fetch(source_id)
            source = self.repository.get_source(source_id)
            assert source is not None
        return source, result

    @staticmethod
    def detect_kind(locator: str) -> SourceKind:
        """Choose the smallest useful connector from a pasted user value."""
        value = locator.strip().casefold()
        if "reddit.com/" in value or re.match(r"^(?:r|u|user)/", value):
            return SourceKind.REDDIT
        if "youtube.com/" in value or "youtu.be/" in value or value.startswith("uc"):
            return SourceKind.YOUTUBE
        if "x.com/" in value or "twitter.com/" in value or value.startswith("@"):
            return SourceKind.X_RSSHUB
        return SourceKind.RSS

    def update_source(self, source_id: int, draft: SourceDraft) -> tuple[dict[str, Any], ValidationResult | None]:
        current = self.repository.get_source(source_id)
        if not current:
            raise ValueError("来源不存在。")
        plugin = self.registry.get(draft.kind)
        locator = plugin.normalize_locator(draft.locator)
        if draft.kind.value != current["kind"] or locator != current["locator"]:
            duplicate = self.repository.find_source(draft.kind.value, locator)
            if duplicate and int(duplicate["id"]) != source_id:
                raise ValueError("这个来源已经存在。")
            # Changing provider identity is intentionally not implicit; it keeps
            # existing historical records attached to the correct source.
            raise ValueError("来源类型或地址已改变，请新增一个来源后再归档旧来源。")
        self.repository.update_source(
            source_id,
            {
                "name": draft.name,
                "description": draft.description.strip()[:300],
                "is_official": int(draft.is_official),
                "enabled": int(draft.enabled),
            },
        )
        if draft.enabled and not current["enabled"]:
            self.repository.schedule_initial_fetch(source_id)
        self.sync_rsshub_runtime()
        source = self.repository.get_source(source_id)
        assert source is not None
        return source, None

    def validate_source(self, source_id: int) -> ValidationResult:
        source = self.repository.get_source(source_id)
        if not source:
            raise ValueError("来源不存在。")
        plugin = self.registry.get(source["kind"])
        try:
            result = plugin.validate(source, self.settings)
        except Exception as exc:
            result = ValidationResult(
                ok=False,
                feed_url=source["feed_url"],
                message=f"连接失败：{exc}",
            )

        if result.ok:
            self.repository.update_source(
                source_id,
                {"feed_url": result.feed_url, "health_status": "healthy", "last_error": ""},
            )
        else:
            self.repository.update_source(
                source_id,
                {"feed_url": result.feed_url, "health_status": "error", "last_error": result.message},
            )
        return result

    def requeue_failed_platform_sources(self, kind: SourceKind | str) -> int:
        """Request a fresh worker check after a shared platform login succeeds."""

        return self.repository.requeue_failed_sources_for_kind(SourceKind(kind).value)

    def archive_source(self, source_id: int) -> None:
        """归档后立刻从 RSSHub 通用 RSS 白名单移除该来源。"""

        self.repository.archive_source(source_id)
        self.sync_rsshub_runtime()

    def sync_rsshub_runtime(self) -> None:
        """同步通用 RSS 白名单；X 凭据由 XSessionService 单独管理。"""

        feed_urls = [
            str(source["locator"])
            for source in self.repository.list_sources()
            if source["kind"] == SourceKind.RSS.value
        ]
        self.runtime_files.write_rss_feed_manifest(feed_urls)

    def refresh_rsshub_feed_urls(self) -> int:
        """将旧数据库里直连的 URL 迁移为当前 RSSHub 路由。"""

        if not self.settings.rsshub_base_url:
            return 0
        updated = 0
        for source in self.repository.list_sources(include_archived=True):
            try:
                desired = self.registry.get(source["kind"]).resolve_feed_url(
                    str(source["locator"]), self.settings
                )
            except Exception:
                # 保留无法自动识别的历史记录，避免启动时破坏已有来源。
                continue
            if desired != source["feed_url"]:
                self.repository.update_source(int(source["id"]), {"feed_url": desired})
                updated += 1
        return updated

    def synchronize_rsshub_sources(self) -> int:
        """启动或导入后统一迁移 URL 并刷新共享运行时清单。"""

        updated = self.refresh_rsshub_feed_urls()
        self.sync_rsshub_runtime()
        return updated

    def queue_sources_for_manual_test(self, source_ids: Sequence[int]) -> int:
        """将当前页仍启用的来源交给下一轮后台抓取验证。"""

        return self.repository.requeue_sources_for_fetch(source_ids)

    def seed_existing_feeds(self) -> int:
        """Import YAML-declared sources without overwriting UI-managed records.

        ``feeds.yml`` is intentionally declarative: it can bootstrap a fresh
        database and safely receive new source rows after the dashboard is
        already in use. Existing source ids are never rewritten here.
        """

        if not self.settings.feeds_path.exists():
            return 0
        with self.settings.feeds_path.open("r", encoding="utf-8") as handle:
            entries = yaml.safe_load(handle) or []
        if not isinstance(entries, list):
            return 0

        created = 0
        global_interval = self.repository.get_fetch_policy().interval_minutes
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                kind = SourceKind(str(entry.get("kind", "rss")))
            except ValueError:
                continue
            locator = str(entry.get("locator") or entry.get("url") or "").strip()
            if not locator:
                continue
            draft = SourceDraft(
                name=str(entry.get("name") or locator),
                kind=kind,
                locator=locator,
                description=str(entry.get("description") or ""),
                is_official=bool(entry.get("official", False)),
                poll_interval_minutes=global_interval,
                enabled=bool(entry.get("enabled", True)),
                archived=bool(entry.get("archived", False)),
            )
            normalized, feed_url = self.prepare_draft(draft)
            if self.repository.find_source(normalized.kind.value, normalized.locator):
                continue
            source_id = self.repository.create_source(normalized, feed_url)
            if normalized.enabled:
                self.repository.schedule_initial_fetch(source_id)
            created += 1
        self.synchronize_rsshub_sources()
        return created

    def form_choices(self) -> list[tuple[str, str]]:
        return [("auto", "自动识别（推荐）"), *self.registry.choices()]
