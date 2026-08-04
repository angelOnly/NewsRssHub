from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.curation import CurationGroup, EditorialTier
from app.domain.models import FeedItem, SourceDraft, SourceKind
from app.runtime import build_services
from app.web import app


def build_settings(root: Path) -> Settings:
    source_dir = root / "sources"
    source_dir.mkdir()
    (source_dir / "user_profile.yml").write_text(
        "identity:\n  description: AI 工程师，关注重要模型和开发工具更新。\n", encoding="utf-8"
    )
    skill = root / ".agents" / "skills" / "curate-personal-news"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# policy\n", encoding="utf-8")
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
    )


class WebTests(unittest.TestCase):
    def test_source_platform_paging_and_batch_add_work_without_an_x_cookie(self) -> None:
        with TemporaryDirectory() as directory:
            services = build_services(build_settings(Path(directory)))
            services.repository.create_source(
                SourceDraft(name="已有 X 来源", kind=SourceKind.X_RSSHUB, locator="ExistingX"),
                "https://x.com/ExistingX",
            )
            for index in range(23):
                services.repository.create_source(
                    SourceDraft(
                        name=f"RSS {index:02d}",
                        kind=SourceKind.RSS,
                        locator=f"https://example.test/{index}.xml",
                    ),
                    f"https://example.test/{index}.xml",
                )
            app.state.services = services
            try:
                with TestClient(app) as client:
                    page = client.get("/sources?kind=rss&page=2")
                    self.assertEqual(page.status_code, 200)
                    self.assertIn("来源管理", page.text)
                    self.assertIn("21–23 条，共 23 条", page.text)
                    self.assertIn('value="rss"', page.text)
                    self.assertIn('name="source_kind" value="rss"', page.text)
                    self.assertNotIn("X 来源暂不测试或抓取", page.text)

                    form = client.get("/sources/batch?kind=x_rsshub")
                    self.assertEqual(form.status_code, 200)
                    self.assertIn("批量添加来源", form.text)
                    self.assertIn("先完成平台连接", form.text)

                    added = client.post(
                        "/sources/batch",
                        data={
                            "kind": "x_rsshub",
                            "entries": "OpenAI | @OpenAI\nAnthropic | @AnthropicAI",
                            "is_official": "true",
                            "poll_interval_minutes": "60",
                            "enabled": "true",
                        },
                    )
                    self.assertEqual(added.status_code, 200)
                    self.assertIn("已添加 2 条", added.text)
                    self.assertIsNotNone(services.repository.find_source("x_rsshub", "OpenAI"))
            finally:
                delattr(app.state, "services")

    def test_four_tier_dashboard_and_source_form_drop_legacy_controls(self) -> None:
        with TemporaryDirectory() as directory:
            services = build_services(build_settings(Path(directory)))
            source_id = services.repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            item_id, _ = services.repository.insert_item(
                source_id,
                FeedItem(
                    guid="one",
                    title="OpenAI 发布新模型",
                    link="https://example.test/one",
                    content="原始内容",
                    published_at=datetime.now(timezone.utc),
                ),
            )
            services.repository.save_item_summary(
                item_id,
                display_title="OpenAI 新模型已开放使用",
                summary="新模型已开放使用。",
                highlights=["开发者现在可以开始使用新模型。"],
                version=2,
            )
            services.repository.save_item_translation(
                item_id, translated_content="这是原始正文的中文译文。"
            )
            event_id = services.repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[item_id],
                        primary_item_id=item_id,
                        tier=EditorialTier.MUST_READ,
                        reason="直接影响当前模型选择",
                        order=1,
                    )
                ]
            )[0]
            app.state.services = services
            try:
                with TestClient(app) as client:
                    health = client.get("/health")
                    self.assertEqual(health.status_code, 200)
                    self.assertEqual(health.json()["database"], "ok")
                    self.assertEqual(health.json()["curation_skill"], "available")

                    dashboard = client.get("/?tier=must_read&period=all")
                    self.assertEqual(dashboard.status_code, 200)
                    self.assertIn('href="/static/app.css?v=', dashboard.text)
                    self.assertIn('href="/static/x-session.css?v=', dashboard.text)
                    self.assertIn('src="/static/scroll-restore.js?v=', dashboard.text)
                    self.assertNotIn('href="http://testserver/static/', dashboard.text)
                    self.assertNotIn("今天，什么真的值得看？", dashboard.text)
                    self.assertNotIn("＋ 添加来源", dashboard.text)
                    self.assertNotIn('aria-label="情报台状态"', dashboard.text)
                    self.assertIn('class="mobile-menu"', dashboard.text)
                    self.assertIn('class="event-preview"', dashboard.text)
                    self.assertIn('aria-label="不感兴趣"', dashboard.text)
                    self.assertIn("必看", dashboard.text)
                    self.assertIn("重要更新", dashboard.text)
                    self.assertIn("资讯速览", dashboard.text)
                    self.assertIn("OpenAI 新模型已开放使用", dashboard.text)
                    self.assertIn("开发者现在可以开始使用新模型", dashboard.text)
                    self.assertNotIn("全部主题", dashboard.text)
                    self.assertNotIn("重要性排序", dashboard.text)

                    detail = client.get(f"/events/{event_id}?tier=must_read&period=all")
                    self.assertEqual(detail.status_code, 200)
                    self.assertIn("原始内容与来源", detail.text)
                    self.assertIn("查看原始正文", detail.text)
                    self.assertIn("中文译文", detail.text)
                    self.assertIn("原始标题", detail.text)
                    self.assertIn("OpenAI 发布新模型", detail.text)
                    self.assertNotIn("模型解读", detail.text)

                    form = client.get("/sources/new")
                    self.assertEqual(form.status_code, 200)
                    self.assertNotIn("优先级（1–10）", form.text)
                    self.assertNotIn("主题", form.text)
            finally:
                delattr(app.state, "services")
