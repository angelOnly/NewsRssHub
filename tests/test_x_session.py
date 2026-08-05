from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.fernet import Fernet
from requests import HTTPError, Response

from app.config import Settings
from app.services.x_session import (
    XCredentialExpiredError,
    XCredentialMissingError,
    XSessionService,
    parse_x_cookie,
)
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
    def test_complete_cookie_is_encrypted_and_written_to_the_shared_runtime_file(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            validations: list[dict[str, object]] = []
            cookie = "auth_token=secret-cookie-value; ct0=csrf-token; twid=u%3D123"

            def validator() -> None:
                validations.append(
                    json.loads(
                        (settings.data_dir / "rsshub-runtime" / "x-twitter.json").read_text(
                            encoding="utf-8"
                        )
                    )
                )

            service = XSessionService(repository, settings, validator=validator)
            status = service.save_from_web(cookie)
            stored = repository.get_connector_credential("x_session")
            assert stored is not None

            expected_header = "auth_token=secret-cookie-value; ct0=csrf-token; twid=u%3D123"
            self.assertEqual(status.state, "valid")
            self.assertEqual(validations, [{"version": 2, "cookie_header": expected_header}])
            self.assertNotIn("secret-cookie-value", stored["ciphertext"])
            self.assertNotIn("csrf-token", stored["ciphertext"])
            runtime_payload = json.loads(
                (settings.data_dir / "rsshub-runtime" / "x-twitter.json").read_text(encoding="utf-8")
            )
            self.assertEqual(runtime_payload, {"version": 2, "cookie_header": expected_header})

            service.test_saved()
            self.assertEqual(len(validations), 2)
            self.assertEqual(validations[1], {"version": 2, "cookie_header": expected_header})

    def test_token_only_input_is_rejected_before_it_can_replace_the_complete_cookie(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            service = XSessionService(repository, settings, validator=lambda: None)
            service.save_from_web("auth_token=known-good-cookie; ct0=known-good-csrf")
            before = repository.get_connector_credential("x_session")
            assert before is not None

            with self.assertRaises(XCredentialMissingError):
                service.save_from_web("auth_token=bad-cookie")

            after = repository.get_connector_credential("x_session")
            assert after is not None
            self.assertEqual(after["ciphertext"], before["ciphertext"])
            self.assertEqual(after["fingerprint"], before["fingerprint"])
            runtime_payload = json.loads(
                (settings.data_dir / "rsshub-runtime" / "x-twitter.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                runtime_payload,
                {
                    "version": 2,
                    "cookie_header": "auth_token=known-good-cookie; ct0=known-good-csrf",
                },
            )

    def test_invalid_complete_cookie_replacement_restores_last_valid_runtime_file(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            valid_service = XSessionService(repository, settings, validator=lambda: None)
            valid_service.save_from_web("auth_token=known-good-cookie; ct0=known-good-csrf")
            before = repository.get_connector_credential("x_session")
            assert before is not None

            invalid_service = XSessionService(
                repository,
                settings,
                validator=lambda: (_ for _ in ()).throw(
                    XCredentialExpiredError("X 登录 Cookie 已失效，请更新完整 Cookie 后重试。")
                ),
            )
            with self.assertRaises(XCredentialExpiredError):
                invalid_service.save_from_web("auth_token=bad-cookie; ct0=bad-csrf")

            after = repository.get_connector_credential("x_session")
            assert after is not None
            self.assertEqual(after["ciphertext"], before["ciphertext"])
            self.assertEqual(after["fingerprint"], before["fingerprint"])
            runtime_payload = json.loads(
                (settings.data_dir / "rsshub-runtime" / "x-twitter.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                runtime_payload,
                {
                    "version": 2,
                    "cookie_header": "auth_token=known-good-cookie; ct0=known-good-csrf",
                },
            )

    def test_startup_restores_complete_cookie_from_the_encrypted_record(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            XSessionService(repository, settings, validator=lambda: None).save_from_web(
                "auth_token=known-good-cookie; ct0=known-good-csrf"
            )
            path = settings.data_dir / "rsshub-runtime" / "x-twitter.json"
            path.unlink()

            XSessionService(repository, settings).sync_runtime_file()

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload,
                {
                    "version": 2,
                    "cookie_header": "auth_token=known-good-cookie; ct0=known-good-csrf",
                },
            )

    def test_legacy_token_record_is_disabled_without_being_sent_to_rsshub(self) -> None:
        with TemporaryDirectory() as directory:
            key = Fernet.generate_key().decode("ascii")
            settings = build_settings(Path(directory), key)
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            ciphertext = Fernet(key.encode("utf-8")).encrypt(
                json.dumps({"auth_token": "legacy-token"}).encode("utf-8")
            ).decode("ascii")
            repository.save_connector_credential(
                connector="x_session",
                ciphertext=ciphertext,
                fingerprint="legacy",
                status="valid",
            )

            service = XSessionService(repository, settings)
            service.sync_runtime_file()

            self.assertFalse((settings.data_dir / "rsshub-runtime" / "x-twitter.json").exists())
            status = service.status()
            self.assertEqual(status.state, "needs_full_cookie")
            self.assertFalse(status.configured)

    def test_rsshub_x_403_marks_the_saved_cookie_invalid(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            service = XSessionService(repository, settings, validator=lambda: None)
            service.save_from_web("auth_token=known-good-cookie; ct0=known-good-csrf")
            response = Response()
            response.status_code = 503
            response._content = b"Error: Twitter API error: 403"
            error = HTTPError("503 Server Error", response=response)

            self.assertTrue(service.record_rsshub_auth_failure(error))
            self.assertEqual(service.status().state, "invalid")
            self.assertFalse(service.record_rsshub_auth_failure(RuntimeError("network timeout")))

    def test_parser_requires_auth_token_and_ct0_from_a_single_line_cookie_header(self) -> None:
        with self.assertRaises(XCredentialMissingError):
            parse_x_cookie("auth_token=only-token")
        with self.assertRaises(XCredentialMissingError):
            parse_x_cookie("ct0=only-csrf")
        with self.assertRaises(XCredentialMissingError):
            parse_x_cookie("auth_token=one;\nct0=two")


if __name__ == "__main__":
    unittest.main()
