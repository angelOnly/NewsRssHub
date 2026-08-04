from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config import Settings
from app.plugins.reddit import RedditSourcePlugin


def build_settings(root: Path, rsshub_base_url: str | None) -> Settings:
    source_dir = root / "sources"
    source_dir.mkdir()
    return Settings(
        root_dir=root,
        source_dir=source_dir,
        data_dir=root / "data",
        database_path=root / "data" / "test.db",
        request_timeout=5,
        log_level="INFO",
        llm_enabled=False,
        openai_api_key=None,
        openai_base_url="https://example.test/v1",
        openai_model_name="test",
        credential_encryption_key=None,
        timezone="Asia/Shanghai",
        rsshub_base_url=rsshub_base_url,
    )


class RedditSourcePluginTests(unittest.TestCase):
    def test_subreddit_and_user_use_the_custom_rsshub_routes(self) -> None:
        with TemporaryDirectory() as directory:
            plugin = RedditSourcePlugin()
            settings = build_settings(Path(directory), "https://rsshub.example.test")

            self.assertEqual(plugin.normalize_locator("https://www.reddit.com/r/OpenAI/.rss"), "r/openai")
            self.assertEqual(
                plugin.resolve_feed_url("r/OpenAI", settings),
                "https://rsshub.example.test/reddit/r/openai",
            )
            self.assertEqual(
                plugin.resolve_feed_url("user/spez", settings),
                "https://rsshub.example.test/reddit/u/spez",
            )

    def test_rsshub_address_is_required(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "rsshub_base_url"):
                RedditSourcePlugin().resolve_feed_url("r/OpenAI", build_settings(Path(directory), None))


if __name__ == "__main__":
    unittest.main()
