from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.runtime import build_services
from app.services.youtube_download import (
    DownloadedYouTubeVideo,
    YouTubeDownloadError,
    YouTubeDownloadService,
    normalize_youtube_video_url,
)
from app.web import app


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
        openai_base_url="https://llm.example.test/v1",
        openai_model_name="test-model",
        credential_encryption_key=None,
        timezone="Asia/Shanghai",
        rsshub_base_url="https://rsshub.example.test",
        youtube_download_timeout_seconds=120,
    )


class YouTubeDownloadServiceTests(unittest.TestCase):
    def test_normalize_accepts_single_video_urls_and_rejects_other_hosts(self) -> None:
        self.assertEqual(
            normalize_youtube_video_url("https://youtu.be/abc_DEF-123?si=unused"),
            ("https://www.youtube.com/watch?v=abc_DEF-123", "abc_DEF-123"),
        )
        self.assertEqual(
            normalize_youtube_video_url("https://www.youtube.com/shorts/abc_DEF-123"),
            ("https://www.youtube.com/watch?v=abc_DEF-123", "abc_DEF-123"),
        )

        for value in (
            "http://youtu.be/abc_DEF-123",
            "https://youtube.com@evil.example/watch?v=abc_DEF-123",
            "https://www.youtube.com:8443/watch?v=abc_DEF-123",
            "https://www.youtube.com/playlist?list=anything",
        ):
            with self.assertRaises(YouTubeDownloadError) as context:
                normalize_youtube_video_url(value)
            self.assertEqual(context.exception.status_code, 422)

    def test_download_uses_a_fixed_single_video_command_and_cleans_up(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            service = YouTubeDownloadService(settings)
            commands: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> SimpleNamespace:
                commands.append(command)
                task_directory = Path(command[command.index("--paths") + 1])
                video = task_directory / "abc_DEF-123.mp4"
                video.write_bytes(b"fake-video")
                # 重复路径不应导致服务误判为多个输出文件。
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{video}\n{video}\n",
                    stderr="",
                )

            with patch("app.services.youtube_download.subprocess.run", side_effect=fake_run):
                downloaded = service.download(
                    "https://www.youtube.com/watch?v=abc_DEF-123&list=ignored"
                )

            self.assertTrue(downloaded.path.is_file())
            self.assertEqual(downloaded.media_type, "video/mp4")
            self.assertEqual(downloaded.download_name, "abc_DEF-123.mp4")
            self.assertEqual(commands[0][-1], "https://www.youtube.com/watch?v=abc_DEF-123")
            self.assertIn("--no-playlist", commands[0])
            self.assertNotIn("--playlist", commands[0])

            service.remove_task(downloaded.task_directory)
            self.assertFalse(downloaded.task_directory.exists())

    def test_timeout_removes_the_partial_task_directory(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            service = YouTubeDownloadService(settings)
            with patch(
                "app.services.youtube_download.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["yt-dlp"], timeout=120),
            ):
                with self.assertRaises(YouTubeDownloadError) as context:
                    service.download("https://youtu.be/abc_DEF-123")

            self.assertEqual(context.exception.status_code, 504)
            root = settings.data_dir / "youtube-downloads"
            self.assertEqual(list(root.iterdir()), [])

    def test_endpoint_returns_a_file_without_a_second_api_key(self) -> None:
        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            services = build_services(settings)
            task_directory = settings.data_dir / "youtube-downloads" / "endpoint-test"
            task_directory.mkdir(parents=True)
            video_path = task_directory / "abc_DEF-123.mp4"
            video_path.write_bytes(b"fake-video")
            downloaded = DownloadedYouTubeVideo(
                path=video_path,
                task_directory=task_directory,
                video_id="abc_DEF-123",
                media_type="video/mp4",
                download_name="abc_DEF-123.mp4",
            )

            app.state.services = services
            try:
                with TestClient(app) as client:
                    with patch.object(services.youtube_downloader, "download", return_value=downloaded):
                        response = client.post(
                            "/api/youtube/download",
                            json={"url": "https://youtu.be/abc_DEF-123"},
                        )
                    invalid_response = client.post(
                        "/api/youtube/download",
                        json={"url": "https://example.com/not-youtube"},
                    )
            finally:
                delattr(app.state, "services")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"fake-video")
            self.assertEqual(response.headers["content-type"], "video/mp4")
            self.assertIn("abc_DEF-123.mp4", response.headers["content-disposition"])
            self.assertFalse(task_directory.exists())
            self.assertEqual(invalid_response.status_code, 422)
