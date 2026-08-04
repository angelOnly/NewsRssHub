from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.fernet import Fernet

from app.config import Settings
from app.services.x_session import XCredentialExpiredError, XSessionService
from app.storage.database import Database
from app.storage.repository import Repository


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
        llm_enabled=False,
        openai_api_key=None,
        openai_base_url="https://example.test/v1",
        openai_model_name="test",
        credential_encryption_key=key,
        timezone="Asia/Shanghai",
        rsshub_base_url="https://rsshub.example.test",
    )


class XSessionTests(unittest.TestCase):
    def test_cookie_is_encrypted_and_written_to_the_shared_runtime_file(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            validations: list[str] = []

            def validator() -> None:
                payload = json.loads(
                    (settings.data_dir / "rsshub-runtime" / "x-twitter.json").read_text(
                        encoding="utf-8"
                    )
                )
                validations.append(payload["auth_token"])

            service = XSessionService(repository, settings, validator=validator)
            status = service.save_from_web("auth_token=secret-cookie-value; ct0=csrf-token")
            stored = repository.get_connector_credential("x_session")
            assert stored is not None

            self.assertEqual(status.state, "valid")
            self.assertEqual(validations, ["secret-cookie-value"])
            self.assertNotIn("secret-cookie-value", stored["ciphertext"])
            runtime_payload = (settings.data_dir / "rsshub-runtime" / "x-twitter.json").read_text(
                encoding="utf-8"
            )
            self.assertIn("secret-cookie-value", runtime_payload)
            self.assertNotIn("csrf-token", runtime_payload)

            service.test_saved()
            self.assertEqual(validations, ["secret-cookie-value", "secret-cookie-value"])

    def test_invalid_replacement_never_overwrites_last_valid_cookie_or_shared_file(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            valid_service = XSessionService(repository, settings, validator=lambda: None)
            valid_service.save_from_web("auth_token=known-good-cookie")
            before = repository.get_connector_credential("x_session")
            assert before is not None

            invalid_service = XSessionService(
                repository,
                settings,
                validator=lambda: (_ for _ in ()).throw(
                    XCredentialExpiredError("X 登录 Cookie 已失效，请更新后重试。")
                ),
            )
            with self.assertRaises(XCredentialExpiredError):
                invalid_service.save_from_web("auth_token=bad-cookie")

            after = repository.get_connector_credential("x_session")
            assert after is not None
            self.assertEqual(after["ciphertext"], before["ciphertext"])
            self.assertEqual(after["fingerprint"], before["fingerprint"])
            runtime_payload = json.loads(
                (settings.data_dir / "rsshub-runtime" / "x-twitter.json").read_text(encoding="utf-8")
            )
            self.assertEqual(runtime_payload["auth_token"], "known-good-cookie")

    def test_startup_restores_runtime_file_from_the_encrypted_record(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            XSessionService(repository, settings, validator=lambda: None).save_from_web(
                "auth_token=known-good-cookie"
            )
            path = settings.data_dir / "rsshub-runtime" / "x-twitter.json"
            path.unlink()

            XSessionService(repository, settings).sync_runtime_file()

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["auth_token"], "known-good-cookie")


if __name__ == "__main__":
    unittest.main()
