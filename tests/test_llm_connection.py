from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.config import Settings
from app.services.llm_connection import LLMConfigurationError, LLMConnectionService
from app.storage.database import Database
from app.storage.repository import Repository


class FakeResponse:
    def __init__(self, status_code: int = 200, content: str = "ok") -> None:
        self.status_code = status_code
        self.content = content

    def json(self) -> dict[str, object]:
        return {"choices": [{"message": {"content": self.content}}]}


def build_settings(root: Path) -> Settings:
    source_dir = root / "sources"
    source_dir.mkdir()
    return Settings(
        root_dir=root,
        source_dir=source_dir,
        data_dir=root / "data",
        database_path=root / "data" / "test.db",
        request_timeout=5,
        log_level="INFO",
        llm_enabled=True,
        openai_api_key="config-secret-value",
        openai_base_url="https://config.example.test/v1",
        openai_model_name="config-model",
        credential_encryption_key=None,
        timezone="Asia/Shanghai",
    )


class LLMConnectionTests(unittest.TestCase):
    def test_runtime_ignores_legacy_sqlite_model_connection(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            # 旧版本写入的记录保留在库中，但绝不能影响当前 Worker 的模型选择。
            repository.save_connector_credential(
                connector="llm_connection",
                ciphertext="legacy-encrypted-payload",
                fingerprint="legacy-key",
                status="valid",
            )
            requests_seen: list[dict[str, object]] = []

            def post(url: str, **kwargs: object) -> FakeResponse:
                requests_seen.append({"url": url, **kwargs})
                return FakeResponse()

            service = LLMConnectionService(repository, settings, request_post=post)
            runtime = service.runtime_config()
            assert runtime is not None
            self.assertEqual(runtime.source, "config")
            self.assertEqual(runtime.base_url, "https://config.example.test/v1")
            self.assertEqual(runtime.model_name, "config-model")

            status = service.test_saved()
            self.assertEqual(status.state, "config")
            self.assertEqual(status.source, "config")
            self.assertEqual(status.model_name, "config-model")
            self.assertEqual(len(requests_seen), 1)
            self.assertEqual(requests_seen[0]["url"], "https://config.example.test/v1/chat/completions")
            body = requests_seen[0]["json"]
            assert isinstance(body, dict)
            self.assertEqual(body["model"], "config-model")
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertIn("messages", body)
            self.assertNotIn("temperature", body)
            self.assertNotIn("top_p", body)
            self.assertNotIn("max_tokens", body)

    def test_web_model_save_is_rejected_without_writing_sqlite(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            service = LLMConnectionService(repository, settings)

            with self.assertRaisesRegex(LLMConfigurationError, "config.yml"):
                service.save_from_web(
                    api_key_value="different-secret",
                    base_url="https://database.example.test/v1",
                    model_name="database-model",
                    enabled=True,
                )

            self.assertIsNone(repository.get_connector_credential("llm_connection"))
