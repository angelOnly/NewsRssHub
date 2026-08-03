from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.fernet import Fernet

from app.config import Settings
from app.services.llm_connection import (
    LLMAuthenticationError,
    LLMConnectionService,
)
from app.storage.database import Database
from app.storage.repository import Repository


class FakeResponse:
    def __init__(self, status_code: int = 200, content: str = '{"ok":true}') -> None:
        self.status_code = status_code
        self.content = content

    def json(self) -> dict[str, object]:
        return {"choices": [{"message": {"content": self.content}}]}


def build_settings(root: Path, key: str) -> Settings:
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
        openai_api_key=None,
        openai_base_url="https://api.example.test/v1",
        openai_model_name="default-model",
        credential_encryption_key=key,
        timezone="Asia/Shanghai",
    )


class LLMConnectionTests(unittest.TestCase):
    def test_web_config_is_encrypted_tested_and_available_to_the_worker(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            requests_seen: list[dict[str, object]] = []

            def post(url: str, **kwargs: object) -> FakeResponse:
                requests_seen.append({"url": url, **kwargs})
                return FakeResponse()

            service = LLMConnectionService(repository, settings, request_post=post)
            status = service.save_from_web(
                api_key_value="model-secret-value",
                base_url="https://gateway.example.test/v1/",
                model_name="news-model",
                enabled=True,
            )

            stored = repository.get_connector_credential("llm_connection")
            assert stored is not None
            self.assertEqual(status.state, "valid")
            self.assertNotIn("model-secret-value", stored["ciphertext"])
            self.assertEqual(service.runtime_config().base_url, "https://gateway.example.test/v1")
            self.assertEqual(service.runtime_config().model_name, "news-model")
            self.assertTrue(service.runtime_config().enabled)
            self.assertEqual(len(requests_seen), 1)
            self.assertEqual(requests_seen[0]["url"], "https://gateway.example.test/v1/chat/completions")
            runtime = service.runtime_config()
            assert runtime is not None
            self.assertEqual(runtime.model_name, "news-model")
            self.assertTrue(runtime.enabled)

    def test_failed_replacement_keeps_the_last_valid_model_connection(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            valid = LLMConnectionService(repository, settings, request_post=lambda *_args, **_kwargs: FakeResponse())
            valid.save_from_web(
                api_key_value="known-good-key",
                base_url="https://gateway.example.test/v1",
                model_name="news-model",
                enabled=True,
            )
            before = repository.get_connector_credential("llm_connection")
            assert before is not None

            rejected = LLMConnectionService(
                repository,
                settings,
                request_post=lambda *_args, **_kwargs: FakeResponse(status_code=401),
            )
            with self.assertRaises(LLMAuthenticationError):
                rejected.save_from_web(
                    api_key_value="bad-key",
                    base_url="https://gateway.example.test/v1",
                    model_name="news-model",
                    enabled=True,
                )

            after = repository.get_connector_credential("llm_connection")
            assert after is not None
            self.assertEqual(after["ciphertext"], before["ciphertext"])
            self.assertEqual(after["fingerprint"], before["fingerprint"])
