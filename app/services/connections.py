"""Platform connection state shared by source setup and collection.

Sources never contain a Cookie or API key themselves.  A source points at a
platform, while this catalog tells the UI and service layer whether that
platform is public or must first have a verified shared connection.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.models import SourceKind
from app.services.x_session import XSessionService


@dataclass(frozen=True, slots=True)
class PlatformConnection:
    key: str
    platform: str
    source_kind: SourceKind
    requires_credentials: bool
    state: str
    usable: bool
    message: str
    setup_url: str = ""
    setup_label: str = ""


class ConnectionRequiredError(ValueError):
    """Raised before a source row is created when its platform is not ready."""

    def __init__(self, connection: PlatformConnection) -> None:
        self.connection = connection
        super().__init__(
            f"添加 {connection.platform} 来源前，请先完成平台连接配置并测试。{connection.message}"
        )


class ConnectionCatalog:
    """A small registry that keeps credential rules out of individual forms.

    A new authenticated platform adds one ``for_kind`` branch plus its own
    credential service.  Public sources remain explicitly marked public, so
    the UI never asks for an unnecessary Cookie.
    """

    def __init__(self, x_sessions: XSessionService | None = None) -> None:
        self.x_sessions = x_sessions

    def source_connections(self) -> list[PlatformConnection]:
        return [
            self.for_kind(SourceKind.X_RSSHUB),
            self.for_kind(SourceKind.REDDIT),
            self.for_kind(SourceKind.RSS),
        ]

    def for_kind(self, kind: SourceKind | str) -> PlatformConnection:
        source_kind = SourceKind(kind)
        if source_kind == SourceKind.X_RSSHUB:
            return self._x_connection()
        if source_kind == SourceKind.REDDIT:
            return PlatformConnection(
                key="reddit_public",
                platform="Reddit",
                source_kind=source_kind,
                requires_credentials=False,
                state="public",
                usable=True,
                message="当前使用公开 RSS，不需要 Cookie 或 API Key。未来启用 Reddit OAuth 时会在此单独配置。",
            )
        return PlatformConnection(
            key="rss_public",
            platform="RSS / Atom",
            source_kind=source_kind,
            requires_credentials=False,
            state="public",
            usable=True,
            message="公开 RSS/Atom 地址不需要平台登录。",
        )

    def ensure_source_ready(self, kind: SourceKind | str) -> PlatformConnection:
        connection = self.for_kind(kind)
        if connection.requires_credentials and not connection.usable:
            raise ConnectionRequiredError(connection)
        return connection

    def _x_connection(self) -> PlatformConnection:
        if not self.x_sessions:
            return PlatformConnection(
                key="x_session",
                platform="X",
                source_kind=SourceKind.X_RSSHUB,
                requires_credentials=True,
                state="missing",
                usable=False,
                message="X 会话服务尚未初始化。",
                setup_url="/settings/x-session",
                setup_label="配置 X Cookie",
            )
        status = self.x_sessions.status()
        return PlatformConnection(
            key="x_session",
            platform="X",
            source_kind=SourceKind.X_RSSHUB,
            requires_credentials=True,
            state=status.state,
            usable=status.state == "valid",
            message=(
                "X Cookie 已验证，可添加多个 X 账号来源。"
                if status.state == "valid"
                else status.message
            ),
            setup_url="/settings/x-session",
            setup_label="配置并测试 X Cookie",
        )
