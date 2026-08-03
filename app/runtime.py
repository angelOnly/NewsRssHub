from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.plugins.registry import build_source_registry
from app.services.pipeline import IntelligencePipeline
from app.services.connections import ConnectionCatalog
from app.services.llm_connection import LLMConnectionService
from app.services.sources import SourceService
from app.services.x_session import XSessionService
from app.storage.database import Database
from app.storage.repository import Repository


@dataclass(slots=True)
class ApplicationServices:
    settings: Settings
    repository: Repository
    pipeline: IntelligencePipeline
    sources: SourceService
    x_sessions: XSessionService
    llm_connections: LLMConnectionService
    connections: ConnectionCatalog


def build_services(settings: Settings | None = None) -> ApplicationServices:
    settings = settings or get_settings()
    database = Database(settings.database_path)
    database.initialize()
    repository = Repository(database)
    x_sessions = XSessionService(repository, settings)
    llm_connections = LLMConnectionService(repository, settings)
    connections = ConnectionCatalog(x_sessions)
    registry = build_source_registry(x_sessions)
    pipeline = IntelligencePipeline(repository, registry, settings, llm_connections, connections)
    source_service = SourceService(repository, registry, settings, connections)
    return ApplicationServices(
        settings=settings,
        repository=repository,
        pipeline=pipeline,
        sources=source_service,
        x_sessions=x_sessions,
        llm_connections=llm_connections,
        connections=connections,
    )
