from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.plugins.registry import build_source_registry
from app.services.pipeline import IntelligencePipeline
from app.services.connections import ConnectionCatalog
from app.services.batch_sources import BatchSourceImportService
from app.services.llm_connection import LLMConnectionService
from app.services.sources import SourceService
from app.services.source_backups import SourceBackupService
from app.services.translator import TranslationService
from app.services.x_session import XSessionService, remove_legacy_sqlite_x_credential
from app.services.web_push import WebPushService
from app.services.youtube_session import YouTubeSessionService
from app.storage.database import Database
from app.storage.repository import Repository
from app.services.youtube_download import YouTubeDownloadService


@dataclass(slots=True)
class ApplicationServices:
    settings: Settings
    repository: Repository
    pipeline: IntelligencePipeline
    sources: SourceService
    x_sessions: XSessionService
    llm_connections: LLMConnectionService
    connections: ConnectionCatalog
    translator: TranslationService
    batch_sources: BatchSourceImportService
    source_backups: SourceBackupService
    web_push: WebPushService
    youtube_sessions: YouTubeSessionService
    youtube_downloader: YouTubeDownloadService


def build_services(settings: Settings | None = None) -> ApplicationServices:
    settings = settings or get_settings()
    database = Database(settings.database_path)
    database.initialize()
    repository = Repository(database)
    # X Cookie 已迁为 RSSHub 共享文件的唯一存储，部署新版本时清除旧密文副本。
    remove_legacy_sqlite_x_credential(repository)
    x_sessions = XSessionService(settings)
    llm_connections = LLMConnectionService(repository, settings)
    connections = ConnectionCatalog(x_sessions, settings.rsshub_base_url)
    registry = build_source_registry()
    source_backups = SourceBackupService(repository, settings)
    web_push = WebPushService(repository, settings)
    youtube_sessions = YouTubeSessionService(settings)
    # 下载器只拿到运行时文件路径；Cookie 内容不会经过 SQLite 或日志。
    youtube_downloader = YouTubeDownloadService(
        settings,
        cookie_file_path=youtube_sessions.cookie_file_path,
    )
    source_service = SourceService(
        repository,
        registry,
        settings,
        connections,
        runtime_files=x_sessions.runtime_files,
    )
    # X Cookie 文件由挂载目录持久化；启动时仅同步 RSS 白名单和历史来源路由。
    source_service.synchronize_rsshub_sources()
    pipeline = IntelligencePipeline(
        repository,
        registry,
        settings,
        llm_connections,
        connections,
        source_backups,
        web_push,
    )
    batch_source_service = BatchSourceImportService(source_service)
    return ApplicationServices(
        settings=settings,
        repository=repository,
        pipeline=pipeline,
        sources=source_service,
        x_sessions=x_sessions,
        llm_connections=llm_connections,
        connections=connections,
        translator=pipeline.translator,
        batch_sources=batch_source_service,
        source_backups=source_backups,
        web_push=web_push,
        youtube_sessions=youtube_sessions,
        youtube_downloader=youtube_downloader,
    )
