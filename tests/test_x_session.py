from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cryptography.fernet import Fernet

from app.config import Settings
from app.domain.models import SourceDraft, SourceKind
from app.services.x_session import XCredentialExpiredError, XSessionService
from app.storage.database import Database
from app.storage.repository import Repository


class FakeClient:
    def __init__(self, cookies: dict[str, str], *, valid: bool = True) -> None:
        self.cookies = dict(cookies)
        self.valid = valid
        self.validate_calls = 0

    def validate(self) -> dict[str, str]:
        self.validate_calls += 1
        if not self.valid:
            raise RuntimeError("401 unauthorized")
        return self.cookies

    def get_user_id(self, _handle: str) -> str:
        return "42"

    def get_user_tweets(self, _user_id: str) -> list[dict[str, object]]:
        return [
            {
                "id": "1700000000000000001",
                "legacy": {
                    "full_text": "A useful X update about a new model.",
                    "created_at": "Sun Aug 03 00:00:00 +0000 2026",
                    "reply_count": 3,
                    "extended_entities": {
                        "media": [
                            {
                                "type": "photo",
                                "media_url_https": "https://pbs.example.test/photo.jpg",
                            },
                            {
                                "type": "video",
                                "media_url_https": "https://pbs.example.test/poster.jpg",
                                "video_info": {
                                    "variants": [
                                        {
                                            "content_type": "application/x-mpegURL",
                                            "url": "https://video.example.test/stream.m3u8",
                                        },
                                        {
                                            "content_type": "video/mp4",
                                            "bitrate": 832000,
                                            "url": "https://video.example.test/stream.mp4",
                                        },
                                    ]
                                },
                            },
                        ]
                    },
                },
            }
        ]

    def close(self) -> None:
        return None


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
    )


class XSessionTests(unittest.TestCase):
    def test_cookie_is_encrypted_and_reused_for_a_batch(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            service = XSessionService(repository, settings, client_factory=FakeClient)

            status = service.save_from_web("auth_token=secret-cookie-value; ct0=csrf-token")
            stored = repository.get_connector_credential("x_session")
            assert stored is not None
            self.assertEqual(status.state, "valid")
            self.assertNotIn("secret-cookie-value", stored["ciphertext"])

            source_id = repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="OpenAI"),
                "https://x.com/OpenAI",
            )
            source = repository.get_source(source_id)
            assert source is not None
            outcome = service.fetch_many([source])[source_id]
            self.assertIsNone(outcome.error)
            self.assertEqual(len(outcome.items), 1)
            self.assertEqual(outcome.items[0].guid, "x:1700000000000000001")
            self.assertEqual(
                outcome.items[0].media,
                [
                    {"kind": "image", "url": "https://pbs.example.test/photo.jpg"},
                    {
                        "kind": "video",
                        "url": "https://video.example.test/stream.mp4",
                        "mime_type": "video/mp4",
                        "poster_url": "https://pbs.example.test/poster.jpg",
                    },
                ],
            )
            self.assertEqual(repository.get_source(source_id)["config"]["x_user_id"], "42")

    def test_x_batch_checks_cookie_once_before_multiple_accounts(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            created_clients: list[FakeClient] = []

            def factory(cookies: dict[str, str]) -> FakeClient:
                client = FakeClient(cookies)
                created_clients.append(client)
                return client

            service = XSessionService(repository, settings, client_factory=factory)
            service.save_from_web("auth_token=known-good-cookie")
            created_clients.clear()
            source_ids = [
                repository.create_source(
                    SourceDraft(name=handle, kind=SourceKind.X_RSSHUB, locator=handle),
                    f"https://x.com/{handle}",
                )
                for handle in ("OpenAI", "AnthropicAI")
            ]
            sources = [repository.get_source(source_id) for source_id in source_ids]
            results = service.fetch_many([source for source in sources if source])

            self.assertTrue(all(results[source_id].error is None for source_id in source_ids))
            self.assertEqual(len(created_clients), 1)
            self.assertEqual(created_clients[0].validate_calls, 1)

    def test_invalid_replacement_never_overwrites_last_valid_cookie(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory), Fernet.generate_key().decode("ascii"))
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            valid_service = XSessionService(repository, settings, client_factory=FakeClient)
            valid_service.save_from_web("auth_token=known-good-cookie")
            before = repository.get_connector_credential("x_session")
            assert before is not None

            invalid_service = XSessionService(
                repository,
                settings,
                client_factory=lambda cookies: FakeClient(cookies, valid=False),
            )
            with self.assertRaises(XCredentialExpiredError):
                invalid_service.save_from_web("auth_token=bad-cookie")

            after = repository.get_connector_credential("x_session")
            assert after is not None
            self.assertEqual(after["ciphertext"], before["ciphertext"])
            self.assertEqual(after["fingerprint"], before["fingerprint"])
