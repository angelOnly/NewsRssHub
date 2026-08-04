from app.plugins.base import PluginRegistry
from app.plugins.reddit import RedditSourcePlugin
from app.plugins.rss import RssSourcePlugin
from app.plugins.x_rsshub import XRsshubSourcePlugin
from app.plugins.youtube import YouTubeSourcePlugin
from app.services.x_session import XSessionService


def build_source_registry(x_sessions: XSessionService | None = None) -> PluginRegistry:
    return PluginRegistry([
        RssSourcePlugin(),
        XRsshubSourcePlugin(x_sessions),
        RedditSourcePlugin(),
        YouTubeSourcePlugin(),
    ])
