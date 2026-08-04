from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.curation import CurationGroup, EditorialTier
from app.domain.models import FeedItem, SourceDraft, SourceKind, ValidationResult
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
    def test_single_source_add_shows_a_clear_result_and_prevents_repeat_submit(self) -> None:
        with TemporaryDirectory() as directory:
            services = build_services(build_settings(Path(directory)))
            successful_validation = ValidationResult(
                ok=True,
                feed_url="https://example.test/feed.xml",
                message="RSS 地址可访问。",
            )
            failed_validation = ValidationResult(
                ok=False,
                feed_url="https://example.test/failed.xml",
                message="远端暂时无法访问。",
            )
            app.state.services = services
            try:
                with TestClient(app) as client:
                    form = client.get("/sources/new")
                    self.assertIn("data-source-submit", form.text)
                    self.assertIn("data-source-submit-status", form.text)
                    self.assertIn('src="/static/source-submit.js?v=', form.text)

                    with patch.object(services.sources, "validate_source", return_value=successful_validation):
                        added = client.post(
                            "/sources/new",
                            data={
                                "name": "Verified RSS",
                                "kind": "rss",
                                "locator": "https://example.test/feed.xml",
                                "enabled": "true",
                            },
                            follow_redirects=False,
                        )
                    self.assertEqual(added.status_code, 303)
                    success_query = parse_qs(urlparse(added.headers["location"]).query)
                    self.assertEqual(success_query["kind"], ["rss"])
                    self.assertEqual(success_query["notice_level"], ["success"])
                    self.assertIn("已添加并验证成功", success_query["notice"][0])
                    self.assertIsNotNone(services.repository.find_source("rss", "https://example.test/feed.xml"))

                    success_page = client.get(added.headers["location"])
                    self.assertIn('class="notice success"', success_page.text)
                    self.assertIn('role="status"', success_page.text)
                    self.assertIn("已添加并验证成功", success_page.text)

                    with patch.object(services.sources, "validate_source", return_value=failed_validation):
                        saved_with_warning = client.post(
                            "/sources/new",
                            data={
                                "name": "Retry RSS",
                                "kind": "rss",
                                "locator": "https://example.test/failed.xml",
                                "enabled": "true",
                            },
                            follow_redirects=False,
                        )
                    warning_query = parse_qs(urlparse(saved_with_warning.headers["location"]).query)
                    self.assertEqual(warning_query["notice_level"], ["warning"])
                    self.assertIn("已添加：Retry RSS", warning_query["notice"][0])
                    self.assertIn("来源已保留", warning_query["notice"][0])
            finally:
                delattr(app.state, "services")

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

                    form = client.get("/sources/batch")
                    self.assertEqual(form.status_code, 200)
                    self.assertIn("批量添加来源", form.text)
                    self.assertIn("下载 YAML 示例", form.text)
                    self.assertIn("下载推荐来源包", form.text)
                    self.assertNotIn("<textarea", form.text)

                    template = client.get("/sources/batch/template.yml")
                    self.assertEqual(template.status_code, 200)
                    self.assertIn("attachment", template.headers["content-disposition"])
                    self.assertIn("sources:", template.text)
                    self.assertIn("x_rsshub", template.text)

                    recommended = client.get("/sources/batch/recommended.yml")
                    self.assertEqual(recommended.status_code, 200)
                    self.assertIn("attachment", recommended.headers["content-disposition"])
                    self.assertIn("Google DeepMind", recommended.text)
                    self.assertIn("Two Minute Papers", recommended.text)

                    added = client.post(
                        "/sources/batch",
                        files={
                            "source_file": (
                                "new-sources.yml",
                                b"""defaults:
  official: true
  enabled: true
sources:
  - name: OpenAI
    kind: x_rsshub
    locator: \"@OpenAI\"
    poll_interval_minutes: 60
  - name: Anthropic
    kind: x_rsshub
    locator: \"@AnthropicAI\"
    poll_interval_minutes: 60
""",
                                "application/x-yaml",
                            )
                        },
                    )
                    self.assertEqual(added.status_code, 200)
                    self.assertIn("已添加 2 条", added.text)
                    self.assertIsNotNone(services.repository.find_source("x_rsshub", "OpenAI"))
            finally:
                delattr(app.state, "services")

    def test_current_platform_page_can_be_queued_for_background_test(self) -> None:
        with TemporaryDirectory() as directory:
            services = build_services(build_settings(Path(directory)))
            source_ids: list[int] = []
            for index in range(23):
                source_id = services.repository.create_source(
                    SourceDraft(
                        name=f"RSS {index:02d}",
                        kind=SourceKind.RSS,
                        locator=f"https://example.test/{index}.xml",
                    ),
                    f"https://example.test/{index}.xml",
                )
                services.repository.update_source(
                    source_id,
                    {
                        "health_status": "error",
                        "last_fetch_at": "2026-08-03T00:00:00+00:00",
                        "last_error": "fetch failed",
                    },
                )
                source_ids.append(source_id)

            expected_page = services.repository.list_sources_page(
                kind=SourceKind.RSS.value,
                page=2,
                page_size=20,
            )
            expected_ids = {int(source["id"]) for source in expected_page.sources}
            app.state.services = services
            try:
                with TestClient(app) as client:
                    page = client.get("/sources?kind=rss&page=2")
                    self.assertEqual(page.status_code, 200)
                    self.assertIn('action="/sources/test-current-page"', page.text)
                    self.assertIn("测试当前页（3）", page.text)

                    queued = client.post(
                        "/sources/test-current-page",
                        data={"source_kind": "rss", "page": "2"},
                        follow_redirects=False,
                    )
                    self.assertEqual(queued.status_code, 303)
                    query = parse_qs(urlparse(queued.headers["location"]).query)
                    self.assertEqual(query["kind"], ["rss"])
                    self.assertEqual(query["page"], ["2"])
                    self.assertIn("notice", query)

                for source_id in source_ids:
                    source = services.repository.get_source(source_id)
                    assert source is not None
                    if source_id in expected_ids:
                        self.assertEqual(source["health_status"], "unknown")
                        self.assertEqual(source["last_error"], "")
                        self.assertIsNone(source["last_fetch_at"])
                    else:
                        self.assertEqual(source["health_status"], "error")
                        self.assertEqual(source["last_error"], "fetch failed")
            finally:
                delattr(app.state, "services")

    def test_current_page_test_is_blocked_without_an_x_cookie_or_platform_tab(self) -> None:
        with TemporaryDirectory() as directory:
            services = build_services(build_settings(Path(directory)))
            source_id = services.repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="OpenAI"),
                "https://x.com/OpenAI",
            )
            services.repository.update_source(
                source_id,
                {
                    "health_status": "error",
                    "last_fetch_at": "2026-08-03T00:00:00+00:00",
                    "last_error": "previous failure",
                },
            )
            app.state.services = services
            try:
                with TestClient(app) as client:
                    x_page = client.get("/sources?kind=x_rsshub")
                    self.assertNotIn('action="/sources/test-current-page"', x_page.text)

                    blocked = client.post(
                        "/sources/test-current-page",
                        data={"source_kind": "x_rsshub", "page": "1"},
                        follow_redirects=False,
                    )
                    self.assertEqual(blocked.status_code, 303)
                    blocked_query = parse_qs(urlparse(blocked.headers["location"]).query)
                    self.assertEqual(blocked_query["kind"], ["x_rsshub"])
                    self.assertIn("error", blocked_query)

                    rejected = client.post(
                        "/sources/test-current-page",
                        data={"source_kind": "all", "page": "1"},
                        follow_redirects=False,
                    )
                    self.assertEqual(rejected.status_code, 303)
                    self.assertIn("error", parse_qs(urlparse(rejected.headers["location"]).query))

                source = services.repository.get_source(source_id)
                assert source is not None
                self.assertEqual(source["health_status"], "error")
                self.assertEqual(source["last_error"], "previous failure")
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
                    self.assertIn('src="/static/read-state.js?v=', dashboard.text)
                    self.assertNotIn('href="http://testserver/static/', dashboard.text)
                    self.assertNotIn("今天，什么真的值得看？", dashboard.text)
                    self.assertNotIn("＋ 添加来源", dashboard.text)
                    self.assertNotIn('aria-label="情报台状态"', dashboard.text)
                    self.assertIn('class="mobile-menu"', dashboard.text)
                    self.assertIn('class="event-preview"', dashboard.text)
                    self.assertIn(f'data-read-event-id="{event_id}"', dashboard.text)
                    self.assertIn('aria-label="不感兴趣"', dashboard.text)
                    self.assertIn("必看", dashboard.text)
                    self.assertIn("重要更新", dashboard.text)
                    self.assertIn("资讯速览", dashboard.text)
                    self.assertIn("OpenAI 新模型已开放使用", dashboard.text)
                    self.assertIn("开发者现在可以开始使用新模型", dashboard.text)
                    self.assertNotIn("全部主题", dashboard.text)
                    self.assertNotIn("重要性排序", dashboard.text)

                    marked = client.post(f"/events/{event_id}/read")
                    self.assertEqual(marked.status_code, 204)
                    read_dashboard = client.get("/?tier=must_read&period=all")
                    self.assertIn('class="event-card is-read"', read_dashboard.text)

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
