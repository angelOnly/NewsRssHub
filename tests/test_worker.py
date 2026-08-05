from __future__ import annotations

import sys
import unittest
from unittest.mock import Mock, patch

from app import worker


class WorkerTests(unittest.TestCase):
    def _services(self) -> Mock:
        services = Mock()
        services.settings.log_level = "INFO"
        services.pipeline.refresh_weekly_topics_once.return_value = {
            "weekly_topics": {"refreshed": True}
        }
        return services

    def test_topics_role_only_runs_the_dedicated_topic_pipeline(self) -> None:
        services = self._services()
        with (
            patch.object(sys, "argv", ["worker", "--role", "topics", "--once"]),
            patch.object(worker, "build_services", return_value=services),
        ):
            self.assertEqual(worker.run(), 0)

        services.pipeline.refresh_weekly_topics_once.assert_called_once_with()
        services.pipeline.process_once.assert_not_called()

    def test_topics_role_uses_the_persisted_minute_interval(self) -> None:
        services = self._services()
        services.repository.get_weekly_topic_refresh_interval_minutes.return_value = 45
        with (
            patch.object(sys, "argv", ["worker", "--role", "topics"]),
            patch.object(worker, "build_services", return_value=services),
            patch.object(worker.time, "sleep", side_effect=KeyboardInterrupt) as sleep,
        ):
            with self.assertRaises(KeyboardInterrupt):
                worker.run()

        sleep.assert_called_once_with(45 * 60)
