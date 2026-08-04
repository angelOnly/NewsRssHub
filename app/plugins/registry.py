from app.plugins.base import PluginRegistry
from app.plugins.reddit import RedditSourcePlugin
from app.plugins.rss import RssSourcePlugin
from app.plugins.x_rsshub import XRsshubSourcePlugin
from app.plugins.youtube import YouTubeSourcePlugin


def build_source_registry() -> PluginRegistry:
    return PluginRegistry([
        RssSourcePlugin(),
        XRsshubSourcePlugin(),
        RedditSourcePlugin(),
        YouTubeSourcePlugin(),
    ])
