from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import Settings
from app.domain.models import FeedItem, SourceKind, ValidationResult


@dataclass(slots=True)
class SourceFetchResult:
    """Result for one source in a connector-level collection pass."""

    items: list[FeedItem] = field(default_factory=list)
    error: Exception | None = None


class SourcePlugin(ABC):
    """Small contract shared by every content connector."""

    kind: SourceKind
    label: str

    @abstractmethod
    def normalize_locator(self, locator: str) -> str:
        """Normalize user input without making a network request."""

    @abstractmethod
    def resolve_feed_url(self, locator: str, settings: Settings) -> str:
        """Turn a locator into the provider's RSS URL."""

    def prepare_source(self, locator: str, settings: Settings) -> tuple[str, str]:
        """Normalize a pasted locator and produce its durable feed address.

        Most connectors can do this without a network request.  A connector
        such as YouTube may resolve a friendly channel handle to its stable
        channel id here, so the database never depends on a mutable handle.
        """

        normalized = self.normalize_locator(locator)
        return normalized, self.resolve_feed_url(normalized, settings)

    @abstractmethod
    def fetch(self, source: dict[str, Any], settings: Settings) -> list[FeedItem]:
        """Fetch and normalize provider data into the common FeedItem shape."""

    def fetch_many(
        self,
        sources: list[dict[str, Any]],
        settings: Settings,
        *,
        wait_between: Callable[[], None] | None = None,
    ) -> dict[int, SourceFetchResult]:
        """Fetch a connector batch.

        Most RSS-like connectors can use the simple sequential default. A
        connector with shared session state can override this to authenticate
        exactly once for the whole batch.
        """

        results: dict[int, SourceFetchResult] = {}
        for index, source in enumerate(sources):
            if index and wait_between:
                wait_between()
            source_id = int(source["id"])
            try:
                results[source_id] = SourceFetchResult(items=self.fetch(source, settings))
            except Exception as exc:
                results[source_id] = SourceFetchResult(error=exc)
        return results

    def validate(self, source: dict[str, Any], settings: Settings) -> ValidationResult:
        feed_url = source.get("feed_url") or self.resolve_feed_url(source["locator"], settings)
        items = self.fetch({**source, "feed_url": feed_url}, settings)
        return ValidationResult(
            ok=True,
            feed_url=feed_url,
            message=f"连接正常，读取到 {len(items)} 条最新内容。",
            item_count=len(items),
        )


class PluginRegistry:
    def __init__(self, plugins: list[SourcePlugin]) -> None:
        self._plugins = {plugin.kind.value: plugin for plugin in plugins}

    def get(self, kind: str | SourceKind) -> SourcePlugin:
        key = kind.value if isinstance(kind, SourceKind) else kind
        try:
            return self._plugins[key]
        except KeyError as exc:
            raise ValueError(f"不支持的来源类型：{key}") from exc

    def choices(self) -> list[tuple[str, str]]:
        return [(kind, plugin.label) for kind, plugin in self._plugins.items()]
