from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from app.config import Settings
from app.runtime import build_services
from app.services.x_session import (
    XCredentialConfigurationError,
    XCredentialExpiredError,
    XCredentialMissingError,
    XSessionService,
    parse_x_cookie,
)


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
        llm_enabled=False,
        openai_api_key=None,
        openai_base_url="https://example.test/v1",
        openai_model_name="test",
        credential_encryption_key=None,
        timezone="Asia/Shanghai",
        rsshub_base_url="https://rsshub.example.test",
    )


class XSessionTests(unittest.TestCase):
    def test_complete_cookie_is_only_written_to_the_shared_runtime_file(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
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

            service = XSessionService(settings, validator=validator)
            status = service.save_from_web(cookie)

            expected_payload = {
                "version": 2,
                "cookie_header": "auth_token=secret-cookie-value; ct0=csrf-token; twid=u%3D123",
            }
            self.assertEqual(status.state, "verified")
            self.assertEqual(validations, [expected_payload])
            self.assertFalse(settings.database_path.exists())
            runtime_payload = json.loads(
                (settings.data_dir / "rsshub-runtime" / "x-twitter.json").read_text(encoding="utf-8")
            )
            self.assertEqual(runtime_payload, expected_payload)
            self.assertEqual(service.status().state, "saved")

    def test_token_only_input_is_rejected_before_it_can_replace_the_complete_cookie(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            service = XSessionService(settings, validator=lambda: None)
            service.save_from_web("auth_token=known-good-cookie; ct0=known-good-csrf")
            path = settings.data_dir / "rsshub-runtime" / "x-twitter.json"
            before = path.read_text(encoding="utf-8")

            with self.assertRaises(XCredentialMissingError):
                service.save_from_web("auth_token=bad-cookie")

            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_invalid_complete_cookie_replacement_restores_last_valid_runtime_file(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            valid_service = XSessionService(settings, validator=lambda: None)
            valid_service.save_from_web("auth_token=known-good-cookie; ct0=known-good-csrf")
            path = settings.data_dir / "rsshub-runtime" / "x-twitter.json"
            before = path.read_text(encoding="utf-8")

            invalid_service = XSessionService(
                settings,
                validator=lambda: (_ for _ in ()).throw(
                    XCredentialExpiredError("X 登录 Cookie 已失效，请更新完整 Cookie 后重试。")
                ),
            )
            with self.assertRaises(XCredentialExpiredError):
                invalid_service.save_from_web("auth_token=bad-cookie; ct0=bad-csrf")

            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_saved_file_is_directly_validated_by_rsshub(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            XSessionService(settings, validator=lambda: None).save_from_web(
                "auth_token=known-good-cookie; ct0=known-good-csrf"
            )
            validations: list[str] = []

            def validator() -> None:
                payload = json.loads(
                    (settings.data_dir / "rsshub-runtime" / "x-twitter.json").read_text(encoding="utf-8")
                )
                validations.append(str(payload["cookie_header"]))

            status = XSessionService(settings, validator=validator).test_saved()

            self.assertEqual(status.state, "verified")
            self.assertEqual(validations, ["auth_token=known-good-cookie; ct0=known-good-csrf"])

    def test_connection_test_calls_the_rsshub_validation_route_not_x_directly(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            service = XSessionService(settings)
            service.runtime_files.write_x_credential(
                parse_x_cookie("auth_token=known-good-cookie; ct0=known-good-csrf")
            )
            response = Mock(ok=True)

            with patch("app.services.x_session.requests.get", return_value=response) as request_get:
                status = service.test_saved()

            self.assertEqual(status.state, "verified")
            request_get.assert_called_once_with(
                "https://rsshub.example.test/newsrsshub/x/validate",
                timeout=5,
                headers={"Accept": "application/json"},
            )

    def test_invalid_runtime_file_is_not_treated_as_a_connected_cookie(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            path = settings.data_dir / "rsshub-runtime" / "x-twitter.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"version": 1, "auth_token": "legacy-token"}), encoding="utf-8")
            service = XSessionService(settings, validator=lambda: None)

            self.assertEqual(service.status().state, "invalid")
            self.assertFalse(service.status().configured)
            with self.assertRaises(XCredentialConfigurationError):
                service.test_saved()

    def test_application_start_removes_legacy_sqlite_x_copy_without_touching_runtime_file(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            initial = build_services(settings)
            initial.repository.save_connector_credential(
                connector="x_session",
                ciphertext="legacy-ciphertext",
                fingerprint="legacy",
                status="valid",
            )
            initial.x_sessions.runtime_files.write_x_credential(
                parse_x_cookie("auth_token=known-good-cookie; ct0=known-good-csrf")
            )

            restarted = build_services(settings)

            self.assertIsNone(restarted.repository.get_connector_credential("x_session"))
            self.assertTrue(restarted.x_sessions.status().configured)

    def test_parser_requires_auth_token_and_ct0_from_a_single_line_cookie_header(self) -> None:
        with self.assertRaises(XCredentialMissingError):
            parse_x_cookie("auth_token=only-token")
        with self.assertRaises(XCredentialMissingError):
            parse_x_cookie("ct0=only-csrf")
        with self.assertRaises(XCredentialMissingError):
            parse_x_cookie("auth_token=one;\nct0=two")


if __name__ == "__main__":
    unittest.main()
