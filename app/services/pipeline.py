from __future__ import annotations

from dataclasses import asdict

from app.config import Settings
from app.plugins.base import PluginRegistry
from app.services.analyzer import AnalysisService
from app.services.briefs import BriefService
from app.services.collector import Collector
from app.services.connections import ConnectionCatalog
from app.services.events import EventService
from app.services.llm_connection import LLMConnectionService
from app.services.sources import SourceService
from app.storage.repository import Repository


class IntelligencePipeline:
    """One durable, reusable pipeline shared by the worker and manual commands."""

    def __init__(
        self,
        repository: Repository,
        registry: PluginRegistry,
        settings: Settings,
        llm_connections: LLMConnectionService | None = None,
        source_connections: ConnectionCatalog | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.settings = settings
        self.llm_connections = llm_connections or LLMConnectionService(repository, settings)
        self.source_connections = source_connections or ConnectionCatalog()
        self.sources = SourceService(repository, registry, settings, self.source_connections)
        self.collector = Collector(repository, registry, EventService(repository), settings)
        self.analyzer = AnalysisService(repository, settings, self.llm_connections)
        self.briefs = BriefService(repository, settings)

    def bootstrap(self) -> int:
        return self.sources.seed_existing_feeds()

    def run_once(self, force: bool = False) -> dict[str, object]:
        collected = self.collector.collect_due_sources(force=force)
        analyzed = self.analyzer.analyze_pending(limit=15)
        brief = self.briefs.generate_today()
        return {
            "collection": asdict(collected),
            "analysis": analyzed,
            "brief": brief,
        }
