from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.config import Settings, load_taxonomy, load_user_profile
from app.domain.scoring import score_item
from app.plugins.base import PluginRegistry, SourceFetchResult
from app.services.events import EventService
from app.storage.repository import Repository, iso_now


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CollectionSummary:
    sources_checked: int = 0
    sources_failed: int = 0
    new_items: int = 0
    events_touched: int = 0


class Collector:
    def __init__(
        self,
        repository: Repository,
        registry: PluginRegistry,
        event_service: EventService,
        settings: Settings,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.event_service = event_service
        self.settings = settings

    def collect_due_sources(self, force: bool = False) -> CollectionSummary:
        profile = load_user_profile(self.settings)
        taxonomy = load_taxonomy(self.settings)
        candidates = self.repository.list_sources() if force else self.repository.due_sources()
        sources = [source for source in candidates if source["enabled"]]
        result = CollectionSummary()

        # A connector can share a credential or connection across many sources.
        # In particular, X validates the Cookie once before collecting all due
        # accounts, rather than sending 42 independent login checks.
        grouped: dict[str, list[dict[str, Any]]] = {}
        for source in sources:
            grouped.setdefault(str(source["kind"]), []).append(source)

        for kind, group in grouped.items():
            plugin = self.registry.get(kind)
            run_ids = {int(source["id"]): self.repository.start_fetch_run(int(source["id"])) for source in group}
            result.sources_checked += len(group)
            try:
                outcomes = plugin.fetch_many(group, self.settings)
            except Exception as exc:  # plugin boundaries must not halt other kinds
                outcomes = {int(source["id"]): SourceFetchResult(error=exc) for source in group}

            for source in group:
                source_id = int(source["id"])
                run_id = run_ids[source_id]
                outcome = outcomes.get(source_id)
                if not outcome:
                    outcome = SourceFetchResult(error=RuntimeError("连接器未返回该来源的抓取结果。"))
                if outcome.error:
                    self._record_failure(source, run_id, outcome.error)
                    result.sources_failed += 1
                    continue

                new_count, event_count = self._record_items(source, run_id, outcome.items, profile, taxonomy)
                result.new_items += new_count
                result.events_touched += event_count

        return result

    def _record_items(
        self,
        source: dict[str, Any],
        run_id: int,
        items: list[Any],
        profile: dict[str, Any],
        taxonomy: dict[str, Any],
    ) -> tuple[int, int]:
        new_count = 0
        event_count = 0
        source_id = int(source["id"])
        for feed_item in items:
            scored = score_item(
                title=feed_item.title,
                content=feed_item.content,
                published_at=feed_item.published_at,
                source_priority=int(source["priority"]),
                is_official=bool(source["is_official"]),
                profile=profile,
                taxonomy=taxonomy,
            )
            item_id, inserted = self.repository.insert_item(
                source_id, feed_item, scored.score, scored.tags, scored.is_blacklisted
            )
            if not inserted:
                continue
            new_count += 1
            if not scored.is_blacklisted and self.event_service.assign_item(item_id):
                event_count += 1

        self.repository.update_source(
            source_id,
            {"health_status": "healthy", "last_fetch_at": iso_now(), "last_success_at": iso_now(), "last_error": ""},
        )
        self.repository.finish_fetch_run(run_id, "success", new_count)
        return new_count, event_count

    def _record_failure(self, source: dict[str, Any], run_id: int, exc: Exception) -> None:
        message = str(exc)[:1000]
        logger.warning("Source fetch failed for %s: %s", source["name"], message)
        self.repository.update_source(
            int(source["id"]),
            {"health_status": "error", "last_fetch_at": iso_now(), "last_error": message},
        )
        self.repository.finish_fetch_run(run_id, "error", 0, message)
