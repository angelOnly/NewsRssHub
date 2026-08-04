"""Batch source setup without tying the web UI to connector details."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.domain.models import SourceDraft, SourceKind
from app.services.sources import SourceService


@dataclass(frozen=True, slots=True)
class BatchSourceRow:
    line_number: int
    name: str
    locator: str
    message: str = ""


@dataclass(slots=True)
class BatchImportResult:
    added: list[BatchSourceRow] = field(default_factory=list)
    duplicates: list[BatchSourceRow] = field(default_factory=list)
    errors: list[BatchSourceRow] = field(default_factory=list)

    @property
    def received_count(self) -> int:
        return len(self.added) + len(self.duplicates) + len(self.errors)


class BatchSourceImportService:
    """Parse human-friendly rows and insert only normalized source records.

    Import intentionally does *not* test or fetch content.  That makes it
    safe to paste X handles before a Cookie is configured, and lets the user
    decide when a platform's manual test should happen.
    """

    MAX_ROWS = 100

    def __init__(self, sources: SourceService) -> None:
        self.sources = sources

    def import_text(
        self,
        *,
        kind: SourceKind | str,
        entries: str,
        is_official: bool = False,
        poll_interval_minutes: int = 60,
        enabled: bool = True,
    ) -> BatchImportResult:
        source_kind = SourceKind(kind)
        parsed_rows = self._parse_rows(source_kind, entries)
        if len(parsed_rows) > self.MAX_ROWS:
            raise ValueError(f"一次最多添加 {self.MAX_ROWS} 条来源，请分批粘贴。")

        result = BatchImportResult()
        seen: set[tuple[str, str]] = set()
        interval = max(5, min(int(poll_interval_minutes), 1440))

        for line_number, name, locator in parsed_rows:
            fallback = BatchSourceRow(line_number=line_number, name=name, locator=locator)
            try:
                draft = SourceDraft(
                    name=name,
                    kind=source_kind,
                    locator=locator,
                    is_official=is_official,
                    poll_interval_minutes=interval,
                    enabled=enabled,
                )
                normalized, feed_url = self.sources.prepare_draft(draft)
                key = (normalized.kind.value, normalized.locator)
                row = BatchSourceRow(
                    line_number=line_number,
                    name=normalized.name,
                    locator=normalized.locator,
                )
                if key in seen:
                    result.duplicates.append(
                        BatchSourceRow(
                            line_number=row.line_number,
                            name=row.name,
                            locator=row.locator,
                            message="与本次粘贴的另一行重复",
                        )
                    )
                    continue
                seen.add(key)
                if self.sources.repository.find_source(*key):
                    result.duplicates.append(
                        BatchSourceRow(
                            line_number=row.line_number,
                            name=row.name,
                            locator=row.locator,
                            message="已经存在于来源库",
                        )
                    )
                    continue
                self.sources.repository.create_source(normalized, feed_url)
                result.added.append(row)
            except Exception as exc:
                result.errors.append(
                    BatchSourceRow(
                        line_number=fallback.line_number,
                        name=fallback.name,
                        locator=fallback.locator,
                        message=str(exc),
                    )
                )
        return result

    def _parse_rows(self, kind: SourceKind, entries: str) -> list[tuple[int, str, str]]:
        rows: list[tuple[int, str, str]] = []
        for line_number, raw in enumerate(entries.splitlines(), start=1):
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            if value.startswith("- "):
                value = value[2:].strip()

            if "|" in value:
                name, locator = (part.strip() for part in value.split("|", 1))
            elif "\t" in value:
                name, locator = (part.strip() for part in value.split("\t", 1))
            else:
                locator = value
                name = self._default_name(kind, locator)

            if not locator:
                rows.append((line_number, name or "未命名来源", ""))
                continue
            rows.append((line_number, (name or self._default_name(kind, locator))[:120], locator))
        if not rows:
            raise ValueError("请至少粘贴一条来源。")
        return rows

    @staticmethod
    def _default_name(kind: SourceKind, locator: str) -> str:
        value = locator.strip().rstrip("/")
        if kind == SourceKind.X_RSSHUB:
            return f"X · {value.lstrip('@')}"
        if kind == SourceKind.REDDIT:
            return f"Reddit · {value.lstrip('/')}"
        if kind == SourceKind.YOUTUBE:
            return f"YouTube · {value.rsplit('/', 1)[-1].lstrip('@')}"
        parsed = urlparse(value)
        return f"RSS · {parsed.netloc or value}"[:120]
