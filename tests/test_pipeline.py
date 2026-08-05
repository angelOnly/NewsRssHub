from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from app.config import Settings
from app.services.pipeline import IntelligencePipeline
from app.services.weekly_topics import DailyTopicRun
from app.storage.database import Database
from app.storage.repository import Repository


class PipelineTests(unittest.TestCase):
    def test_each_pipeline_pass_runs_content_cleanup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "sources"
            source_dir.mkdir()
            settings = Settings(
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
            )
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            pipeline = IntelligencePipeline(repository, Mock(), settings)
            cleanup_result = {"briefs": 1, "events": 2, "items": 3}

            with patch.object(
                repository, "purge_expired_content", return_value=cleanup_result
            ) as cleanup:
                outcome = pipeline.run_once()

            cleanup.assert_called_once_with()
            self.assertEqual(outcome["cleanup"], cleanup_result)

    def test_daily_topics_refresh_runs_outside_the_content_processor(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "sources"
            source_dir.mkdir()
            settings = Settings(
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
            )
            repository = Repository(Database(settings.database_path))
            repository.database.initialize()
            pipeline = IntelligencePipeline(repository, Mock(), settings)

            with patch.object(
                pipeline.daily_topics,
                "refresh_current_day",
                return_value=DailyTopicRun(refreshed=True, topics=1, events=2),
            ) as refresh:
                processor_outcome = pipeline.process_once()
                refresh.assert_not_called()

                topic_outcome = pipeline.refresh_daily_topics_once()

            self.assertNotIn("daily_topics", processor_outcome)
            self.assertEqual(
                topic_outcome,
                {"daily_topics": {"refreshed": True, "topics": 1, "events": 2, "skipped": False, "failed": False, "message": ""}},
            )
