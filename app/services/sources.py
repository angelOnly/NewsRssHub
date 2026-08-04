from __future__ import annotations

from dataclasses import replace
import re
from typing import Any

import yaml

from app.config import Settings
from app.domain.models import SourceDraft, SourceKind, ValidationResult
from app.plugins.base import PluginRegistry
from app.services.connections import ConnectionCatalog
from app.storage.repository import Repository


class SourceService:
    def __init__(
        self,
        repository: Repository,
        registry: PluginRegistry,
        settings: Settings,
        connections: ConnectionCatalog | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.settings = settings
        self.connections = connections or ConnectionCatalog()

    def prepare_draft(self, draft: SourceDraft) -> tuple[SourceDraft, str]:
        """Turn user input into one normalized, durable source draft."""

        plugin = self.registry.get(draft.kind)
        locator, feed_url = plugin.prepare_source(draft.locator, self.settings)
        return replace(draft, locator=locator), feed_url

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
        source = self.repository.get_source(source_id)
        assert source is not None

        result: ValidationResult | None = None
        if validate:
            result = self.validate_source(source_id)
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
                "is_official": int(draft.is_official),
                "enabled": int(draft.enabled),
                "poll_interval_minutes": draft.poll_interval_minutes,
                "fallback_url": draft.fallback_url,
            },
        )
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
            try:
                poll_interval = max(5, min(int(entry.get("poll_interval_minutes", 120)), 1440))
            except (TypeError, ValueError):
                continue
            draft = SourceDraft(
                name=str(entry.get("name") or locator),
                kind=kind,
                locator=locator,
                is_official=bool(entry.get("official", False)),
                poll_interval_minutes=poll_interval,
                fallback_url=str(entry.get("fallback_url") or ""),
                enabled=bool(entry.get("enabled", True)),
            )
            normalized, feed_url = self.prepare_draft(draft)
            if self.repository.find_source(normalized.kind.value, normalized.locator):
                continue
            self.repository.create_source(normalized, feed_url)
            created += 1
        return created

    def form_choices(self) -> list[tuple[str, str]]:
        return [("auto", "自动识别（推荐）"), *self.registry.choices()]
