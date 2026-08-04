from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.config import Settings
from app.domain.models import FetchPolicy
from app.plugins.base import PluginRegistry, SourceFetchResult
from app.services.connections import ConnectionCatalog
from app.storage.repository import Repository, iso_now


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CollectionSummary:
    sources_checked: int = 0
    sources_failed: int = 0
    new_items: int = 0
    sources_scheduled: int = 0


class Collector:
    def __init__(
        self,
        repository: Repository,
        registry: PluginRegistry,
        settings: Settings,
        connections: ConnectionCatalog | None = None,
        sleeper: Callable[[float], None] | None = None,
        delay_provider: Callable[[], float] | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.settings = settings
        self.connections = connections or ConnectionCatalog()
        self._sleep = sleeper or time.sleep
        self._delay_provider = delay_provider or (lambda: random.uniform(2.0, 5.0))

    def _wait_between_sources(self) -> None:
        """所有平台统一按来源错开请求，降低共享出口的瞬时压力。"""

        self._sleep(max(0.0, float(self._delay_provider())))

    def collect_due_sources(self, force: bool = False) -> CollectionSummary:
        policy = self.repository.get_fetch_policy()
        result = CollectionSummary()
        if not force:
            result.sources_scheduled = self.repository.schedule_unplanned_sources(policy)
        candidates = self.repository.list_sources() if force else self.repository.due_sources()
        sources = [
            source
            for source in candidates
            if source["enabled"] and self.connections.for_kind(source["kind"]).usable
        ]
        # A connector can share a credential or connection across many sources.
        # In particular, X validates the Cookie once before collecting all due
        # accounts, rather than sending 42 independent login checks.
        grouped: dict[str, list[dict[str, Any]]] = {}
        for source in sources:
            grouped.setdefault(str(source["kind"]), []).append(source)

        for group_index, (kind, group) in enumerate(grouped.items()):
            # 插件只负责同平台批次内的等待；切换平台时也要留出间隔，
            # 这样整轮中相邻的两次来源请求都会错开。
            if group_index:
                self._wait_between_sources()
            plugin = self.registry.get(kind)
            result.sources_checked += len(group)
            try:
                outcomes = plugin.fetch_many(
                    group,
                    self.settings,
                    wait_between=self._wait_between_sources,
                )
            except Exception as exc:  # plugin boundaries must not halt other kinds
                outcomes = {int(source["id"]): SourceFetchResult(error=exc) for source in group}

            for source in group:
                source_id = int(source["id"])
                outcome = outcomes.get(source_id)
                if not outcome:
                    outcome = SourceFetchResult(error=RuntimeError("连接器未返回该来源的抓取结果。"))
                if outcome.error:
                    self._record_failure(source, outcome.error, policy)
                    result.sources_failed += 1
                    continue

                new_count = self._record_items(source, outcome.items, policy)
                result.new_items += new_count

        return result

    def _record_items(
        self,
        source: dict[str, Any],
        items: list[Any],
        policy: FetchPolicy,
    ) -> int:
        new_count = 0
        source_id = int(source["id"])
        for feed_item in items:
            _, inserted = self.repository.insert_item(source_id, feed_item)
            if not inserted:
                continue
            new_count += 1

        completed_at = iso_now()
        self.repository.update_source(
            source_id,
            {
                "health_status": "healthy",
                "last_fetch_at": completed_at,
                "last_success_at": completed_at,
                "last_new_item_count": new_count,
                "last_error": "",
            },
        )
        self.repository.schedule_next_fetch(source_id, policy)
        return new_count

    def _record_failure(self, source: dict[str, Any], exc: Exception, policy: FetchPolicy) -> None:
        message = str(exc)[:1000]
        logger.warning("Source fetch failed for %s: %s", source["name"], message)
        self.repository.update_source(
            int(source["id"]),
            {
                "health_status": "error",
                "last_fetch_at": iso_now(),
                "last_new_item_count": 0,
                "last_error": message,
            },
        )
        # 失败同样进入下一轮排期，避免网络抖动或限流时无限立即重试。
        self.repository.schedule_next_fetch(int(source["id"]), policy)
