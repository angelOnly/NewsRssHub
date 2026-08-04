from __future__ import annotations

from dataclasses import asdict

from app.config import Settings
from app.plugins.base import PluginRegistry
from app.services.briefs import BriefService
from app.services.collector import Collector
from app.services.connections import ConnectionCatalog
from app.services.curator import CurationService
from app.services.llm_connection import LLMConnectionService
from app.services.skill_loader import SkillLoader
from app.services.sources import SourceService
from app.services.summarizer import SummaryService
from app.services.translator import TranslationService
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
        self.source_connections = source_connections or ConnectionCatalog(
            rsshub_base_url=settings.rsshub_base_url
        )
        self.sources = SourceService(repository, registry, settings, self.source_connections)
        self.collector = Collector(repository, registry, settings, self.source_connections)
        self.summarizer = SummaryService(repository, settings, self.llm_connections)
        self.skill_loader = SkillLoader(settings)
        self.curator = CurationService(
            repository, settings, self.llm_connections, self.skill_loader
        )
        self.translator = TranslationService(repository, settings, self.llm_connections)
        self.briefs = BriefService(repository, settings)

    def bootstrap(self) -> int:
        return self.sources.seed_existing_feeds()

    def run_once(self, force: bool = False) -> dict[str, object]:
        collected = self.collector.collect_due_sources(force=force)
        summarized = self.summarizer.summarize_pending(limit=50)
        curated = self.curator.curate_available(limit=120)
        translated = self.translator.translate_visible_primary_items(limit=12)
        brief = self.briefs.generate_today()
        return {
            "collection": asdict(collected),
            "summary": asdict(summarized),
            "curation": asdict(curated),
            "translation": asdict(translated),
            "brief": brief,
        }
