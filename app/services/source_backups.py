"""来源列表的可下载导出与宿主机定期备份。"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from app.config import Settings
from app.storage.repository import Repository


@dataclass(frozen=True, slots=True)
class SourceBackup:
    """一个已经落在持久化目录中的来源快照。"""

    filename: str
    created_at: str
    source_count: int


class SourceBackupService:
    """只备份用户维护的来源配置，不备份任何凭证或运行状态。"""

    INTERVAL = timedelta(days=3)
    RETAIN_COUNT = 5
    _FILENAME = re.compile(r"sources-\d{8}T\d{12}Z\.yml")

    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    @property
    def directory(self) -> Path:
        """Docker 中的 /app/data 会映射到宿主机的持久化 data 目录。"""

        return self.settings.data_dir / "source_backups"

    def export_text(self, *, now: datetime | None = None) -> str:
        """生成可直接重新上传的 YAML，不包含 Cookie、密钥或连接器缓存。"""

        now = self._utc(now)
        sources = [self._export_source(source) for source in self.repository.list_sources(include_archived=True)]
        payload = {
            "version": 1,
            "exported_at": now.isoformat(),
            "sources": sources,
        }
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)

    def create_backup(self, *, now: datetime | None = None) -> SourceBackup:
        """原子写入一份快照，并清理同一持久化目录中过期的旧快照。"""

        now = self._utc(now)
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"sources-{now.strftime('%Y%m%dT%H%M%S%fZ')}.yml"
        destination = directory / filename
        temporary = directory / f".{filename}.{uuid.uuid4().hex}.tmp"
        content = self.export_text(now=now)
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        self._trim_backups()
        return SourceBackup(filename=filename, created_at=now.isoformat(), source_count=self._source_count(content))

    def ensure_periodic_backup(self, *, now: datetime | None = None) -> SourceBackup | None:
        """每三天最多生成一次；首次运行会立即建立第一份保护快照。"""

        now = self._utc(now)
        backups = self._backup_paths()
        if backups:
            # 以文件名中的生成时刻为准，而不是宿主机可能被复制或修改过的 mtime。
            latest_created_at = self._filename_time(backups[0])
            if now - latest_created_at < self.INTERVAL:
                return None
        return self.create_backup(now=now)

    def list_backups(self) -> list[SourceBackup]:
        """列出可下载快照；读坏文件时仍保留其下载入口，方便人工恢复。"""

        result: list[SourceBackup] = []
        for path in self._backup_paths()[: self.RETAIN_COUNT]:
            fallback_time = self._filename_time(path).isoformat()
            try:
                content = path.read_text(encoding="utf-8")
                document = yaml.safe_load(content) or {}
                if not isinstance(document, dict):
                    raise ValueError("备份根节点不是对象")
                created_at = str(document.get("exported_at") or fallback_time)
                sources = document.get("sources")
                source_count = len(sources) if isinstance(sources, list) else 0
            except (OSError, ValueError, yaml.YAMLError):
                created_at = fallback_time
                source_count = 0
            result.append(
                SourceBackup(
                    filename=path.name,
                    created_at=created_at,
                    source_count=source_count,
                )
            )
        return result

    def get_backup(self, filename: str) -> Path | None:
        """仅允许下载本服务生成的文件名，避免 Web 路径穿越。"""

        if not self._FILENAME.fullmatch(filename):
            return None
        candidate = self.directory / filename
        return candidate if candidate.is_file() else None

    def _backup_paths(self) -> list[Path]:
        directory = self.directory
        if not directory.is_dir():
            return []
        return sorted(
            (path for path in directory.glob("sources-*.yml") if self._FILENAME.fullmatch(path.name)),
            key=lambda path: path.name,
            reverse=True,
        )

    def _trim_backups(self) -> None:
        """按用户要求只保留最近五份，删除目标严格限制在备份目录。"""

        for path in self._backup_paths()[self.RETAIN_COUNT :]:
            path.unlink(missing_ok=True)

    @staticmethod
    def _export_source(source: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": str(source["name"]),
            "description": str(source.get("description") or ""),
            "kind": str(source["kind"]),
            "locator": str(source["locator"]),
            "official": bool(source["is_official"]),
            "enabled": bool(source["enabled"] and not source["archived"]),
            "archived": bool(source["archived"]),
        }

    @staticmethod
    def _source_count(content: str) -> int:
        document = yaml.safe_load(content) or {}
        sources = document.get("sources") if isinstance(document, dict) else None
        return len(sources) if isinstance(sources, list) else 0

    @staticmethod
    def _filename_time(path: Path) -> datetime:
        """从服务自己生成的固定格式文件名恢复 UTC 生成时间。"""

        value = path.name.removeprefix("sources-").removesuffix(".yml")
        return datetime.strptime(value, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)

    @staticmethod
    def _utc(value: datetime | None) -> datetime:
        current = value or datetime.now(timezone.utc)
        return current.astimezone(timezone.utc)
