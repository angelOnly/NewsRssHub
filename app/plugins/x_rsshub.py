from __future__ import annotations

import re
from typing import Any, Callable

from app.config import Settings
from app.domain.models import FeedItem, SourceKind, ValidationResult
from app.plugins.base import SourceFetchResult, SourcePlugin
from app.services.x_session import XSessionService


class XRsshubSourcePlugin(SourcePlugin):
    """X source via a dynamically managed X session.

    ``x_rsshub`` remains the persisted kind for backwards compatibility with
    existing source rows. RSSHub cannot reload its Twitter credentials at
    runtime, so this connector owns the user-managed session instead.
    """

    kind = SourceKind.X_RSSHUB
    label = "X 账号（动态会话）"

    def __init__(self, sessions: XSessionService | None = None) -> None:
        self.sessions = sessions

    def normalize_locator(self, locator: str) -> str:
        value = locator.strip().rstrip("/")
        value = re.sub(r"^https?://(?:www\.)?(?:x|twitter)\.com/", "", value, flags=re.IGNORECASE)
        value = value.lstrip("@").split("/", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", value):
            raise ValueError("请输入 X 用户名（例如 @OpenAI）或其主页地址。")
        return value

    def resolve_feed_url(self, locator: str, settings: Settings) -> str:
        return f"https://x.com/{self.normalize_locator(locator)}"

    def fetch(self, source: dict[str, Any], settings: Settings) -> list[FeedItem]:
        result = self.fetch_many([source], settings).get(int(source["id"]))
        if not result:
            raise RuntimeError("X 连接器未返回抓取结果。")
        if result.error:
            raise result.error
        return result.items

    def validate(self, source: dict[str, Any], settings: Settings) -> ValidationResult:
        items = self.fetch(source, settings)
        return ValidationResult(
            ok=True,
            feed_url=self.resolve_feed_url(str(source["locator"]), settings),
            message=f"X 登录状态正常，读取到 {len(items)} 条最新内容。",
            item_count=len(items),
        )

    def fetch_many(
        self,
        sources: list[dict[str, Any]],
        settings: Settings,
        *,
        wait_between: Callable[[], None] | None = None,
    ) -> dict[int, SourceFetchResult]:
        if not self.sessions:
            error = RuntimeError("X 会话服务尚未初始化。")
            return {int(source["id"]): SourceFetchResult(error=error) for source in sources}
        return self.sessions.fetch_many(sources, wait_between=wait_between)
