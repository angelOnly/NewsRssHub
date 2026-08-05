from __future__ import annotations

import logging
from dataclasses import asdict

from app.config import Settings
from app.plugins.base import PluginRegistry
from app.services.collector import Collector
from app.services.connections import ConnectionCatalog
from app.services.curator import CurationService
from app.services.llm_connection import LLMConnectionService
from app.services.skill_loader import SkillLoader
from app.services.source_backups import SourceBackupService
from app.services.sources import SourceService
from app.services.summarizer import SummaryService
from app.services.translator import TranslationService
from app.services.web_push import WebPushService
from app.services.weekly_topics import WeeklyTopicService
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
        source_backups: SourceBackupService | None = None,
        web_push: WebPushService | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.settings = settings
        self.llm_connections = llm_connections or LLMConnectionService(repository, settings)
        self.source_connections = source_connections or ConnectionCatalog(
            rsshub_base_url=settings.rsshub_base_url
        )
        self.sources = SourceService(repository, registry, settings, self.source_connections)
        self.source_backups = source_backups or SourceBackupService(repository, settings)
        self.collector = Collector(repository, registry, settings, self.source_connections)
        self.summarizer = SummaryService(repository, settings, self.llm_connections)
        self.skill_loader = SkillLoader(settings)
        self.curator = CurationService(
            repository, settings, self.llm_connections, self.skill_loader
        )
        self.translator = TranslationService(repository, settings, self.llm_connections)
        self.weekly_topics = WeeklyTopicService(repository, settings, self.llm_connections)
        self.web_push = web_push or WebPushService(repository, settings)

    def bootstrap(self) -> int:
        return self.sources.seed_existing_feeds()

    def collect_once(self, force: bool = False) -> dict[str, object]:
        """执行来源快照与到期抓取；模型处理不会拖慢调度检查。"""

        try:
            source_backup = self.source_backups.ensure_periodic_backup()
        except OSError:
            # 备份目录异常不能阻断正常抓取；详细原因保留在 Worker 日志中。
            logging.getLogger(__name__).exception("来源自动备份失败，本轮抓取继续执行")
            source_backup = None
        collected = self.collector.collect_due_sources(force=force)
        return {
            "collection": asdict(collected),
            "source_backup": source_backup.filename if source_backup else None,
        }

    def process_once(self) -> dict[str, object]:
        """处理积压内容，并在处理完成后执行内容保留期清理。"""

        summarized = self.summarizer.summarize_pending(limit=50)
        curated = self.curator.curate_available(limit=120)
        # 只在新闻已经完成摘要和筛选、首页可见后才进入通知队列。
        push_queued = self.web_push.record_ready_items(curated.completed)
        translated = self.translator.translate_visible_primary_items(limit=12)
        weekly_topics = self.weekly_topics.refresh_current_week()
        cleanup = self.repository.purge_expired_content()
        # 处理完成后再提醒，用户点开首页时能直接看到本轮已落库的内容。
        push_delivery = self.web_push.deliver_pending()
        return {
            "summary": asdict(summarized),
            "curation": asdict(curated),
            "translation": asdict(translated),
            "weekly_topics": asdict(weekly_topics),
            "cleanup": cleanup,
            "push_queued": push_queued,
            "web_push": asdict(push_delivery),
        }

    def run_once(self, force: bool = False) -> dict[str, object]:
        """兼容旧命令：先抓取，再处理积压内容。"""

        return {**self.collect_once(force=force), **self.process_once()}
