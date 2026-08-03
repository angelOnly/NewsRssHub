from __future__ import annotations

from datetime import datetime, timezone
import unittest

from app.domain.scoring import score_item


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = {
            "interests": [{"name": "AI产品与行业前沿", "weight": 10}],
            "scoring": {"high_weight_multiplier": 1.5},
            "blacklist": ["娱乐八卦"],
        }
        self.taxonomy = {"topics": {"AI产品与行业前沿": {"keywords": ["OpenAI", "model release"]}}}

    def test_matching_official_item_scores_higher(self) -> None:
        result = score_item(
            title="OpenAI model release", content="New model available",
            published_at=datetime.now(timezone.utc), source_priority=9, is_official=True,
            profile=self.profile, taxonomy=self.taxonomy,
        )
        self.assertGreaterEqual(result.score, 50)
        self.assertEqual(result.tags, ["AI产品与行业前沿"])

    def test_blacklist_excludes_item(self) -> None:
        result = score_item(
            title="娱乐八卦", content="OpenAI", published_at=None, source_priority=10,
            is_official=True, profile=self.profile, taxonomy=self.taxonomy,
        )
        self.assertTrue(result.is_blacklisted)
        self.assertEqual(result.score, 0)
