from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.curation import CurationGroup, EditorialTier
from app.domain.models import FeedItem, SourceDraft, SourceKind, ValidationResult
from app.runtime import build_services
from app.web import _compact_relative_time, app, templates


def build_settings(root: Path) -> Settings:
    source_dir = root / "sources"
    source_dir.mkdir()
    (source_dir / "user_profile.yml").write_text(
        "identity:\n  description: AI 工程师，关注重要模型和开发工具更新。\n", encoding="utf-8"
    )
    skill = root / ".agents" / "skills" / "curate-personal-news"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# policy\n", encoding="utf-8")
    topic_skill = root / ".agents" / "skills" / "weekly-hot-topics"
    topic_skill.mkdir(parents=True)
    (topic_skill / "SKILL.md").write_text("# weekly topic policy\n", encoding="utf-8")
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
    )


class WebTests(unittest.TestCase):
    def test_compact_relative_time_is_suited_to_source_cards(self) -> None:
        now = datetime.now(timezone.utc)

        self.assertEqual(_compact_relative_time((now - timedelta(minutes=47)).isoformat()), "47 min")
        self.assertEqual(_compact_relative_time((now + timedelta(minutes=2)).isoformat()), "2 min")
        self.assertEqual(_compact_relative_time((now - timedelta(hours=3)).isoformat()), "3 h")

    def test_web_x_cookie_save_writes_the_shared_runtime_file(self) -> None:
        """设置页保存成功后，RSSHub 可立即读取同一数据卷中的最小凭据文件。"""

        with TemporaryDirectory() as directory:
            settings = build_settings(Path(directory))
            services = build_services(settings)
            app.state.services = services
            try:
                with TestClient(app) as client:
                    # 此用例只验证 Web 到共享文件的调用链，RSSHub 本身由独立集成测试验证。
                    with patch.object(services.x_sessions, "_validate_runtime_credential"):
                        response = client.post(
                            "/settings/x-session",
                            data={"cookie_value": "auth_token=web-test-token; ct0=csrf-token"},
                            follow_redirects=False,
                        )
                    settings_page = client.get("/settings")

                self.assertEqual(response.status_code, 303)
                self.assertIn("Cookie 已保存", settings_page.text)
                self.assertIn("不写入 SQLite", settings_page.text)
                payload = json.loads(
                    (settings.data_dir / "rsshub-runtime" / "x-twitter.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    payload,
                    {
                        "version": 2,
                        "cookie_header": "auth_token=web-test-token; ct0=csrf-token",
                    },
                )
                self.assertIsNone(services.repository.get_connector_credential("x_session"))
            finally:
                delattr(app.state, "services")

    def test_mobile_navigation_and_daily_topic_markup_are_present(self) -> None:
        navigation = templates.get_template("base.html").render(request=object(), active_path="/daily-topics")
        daily_page = templates.get_template("daily_topics.html").render(
            request=object(),
            active_path="/daily-topics",
            topic_date=datetime(2026, 8, 5).date(),
            topic_skill_status=SimpleNamespace(available=True, message=""),
            topics=[
                {
                    "id": 42,
                    "display_name": "MiniMax-M3 发布与评测",
                    "content_count": 8,
                    "description": "围绕 MiniMax-M3 的发布信息与实测结果。",
                    "events": [
                        {
                            "id": 11,
                            "title": "MiniMax-M3 发布",
                            "content_count": 4,
                            "editorial_tier": "important",
                        }
                    ],
                }
            ],
        )

        self.assertIn('class="mobile-primary-nav"', navigation)
        self.assertIn('href="/daily-topics" aria-current="page"', navigation)
        self.assertNotIn('href="/briefs"', navigation)
        self.assertNotIn('class="mobile-menu"', navigation)
        self.assertIn('data-topic-id="42"', daily_page)
        self.assertIn('id="topic-42"', daily_page)
        self.assertIn('href="/daily-topics/42"', daily_page)
        self.assertIn('class="weekly-topic-card weekly-topic-card-link"', daily_page)
        self.assertIn("MiniMax-M3 发布与评测", daily_page)
        self.assertIn('class="daily-topic-content-count">8 条内容</span>', daily_page)
        self.assertIn("围绕 MiniMax-M3 的发布信息与实测结果。", daily_page)
        self.assertNotIn("3 个事件", daily_page)
        self.assertNotIn("4 个来源", daily_page)
        self.assertIn('class="daily-topic-description"', daily_page)
        self.assertNotIn('class="weekly-topic-kicker"', daily_page)
        self.assertNotIn('class="weekly-topic-events"', daily_page)

    def test_dashboard_hides_topic_strip_and_labels_matching_events(self) -> None:
        with TemporaryDirectory() as directory:
            services = build_services(build_settings(Path(directory)))
            app.state.services = services
            event = {
                "id": 42,
                "title": "MiniMax-M3 发布",
                "primary_source_name": "RSS",
                "latest_published_at": None,
                "latest_fetched_at": None,
                "visible_item_count": 1,
                "visible_source_count": 1,
                "user_read": False,
                "user_saved": False,
                "user_hidden": False,
                "summary": None,
                "highlights": [],
                "tier_reason": None,
            }
            try:
                with patch.object(services.repository, "list_events", return_value=[event]), patch.object(
                    services.repository,
                    "list_daily_topic_names_for_events",
                    return_value={42: "MiniMax-M3 发布与评测"},
                ) as topic_query:
                    with TestClient(app) as client:
                        response = client.get("/?tier=must_read&period=24h")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(topic_query.call_args.kwargs["event_ids"], [42])
                self.assertNotIn('class="dashboard-topic-strip"', response.text)
                self.assertIn('class="daily-topic-badge"', response.text)
                self.assertIn("MiniMax-M3 发布与评测", response.text)
            finally:
                delattr(app.state, "services")

    def test_daily_topics_route_renders_the_current_day_empty_state_and_old_route_redirects(self) -> None:
        with TemporaryDirectory() as directory:
            services = build_services(build_settings(Path(directory)))
            app.state.services = services
            try:
                with TestClient(app) as client:
                    response = client.get("/daily-topics")
                    old_route = client.get("/weekly-topics", follow_redirects=False)

                self.assertEqual(response.status_code, 200)
                self.assertIn("今日热点", response.text)
                self.assertIn("今日暂无热点话题", response.text)
                self.assertNotIn("今日话题暂未更新", response.text)
                self.assertEqual(old_route.status_code, 307)
                self.assertEqual(old_route.headers["location"], "/daily-topics")
            finally:
                delattr(app.state, "services")

    def test_daily_topic_card_opens_a_detail_page_with_its_events(self) -> None:
        with TemporaryDirectory() as directory:
            services = build_services(build_settings(Path(directory)))
            app.state.services = services
            topic = {
                "id": 42,
                "display_name": "MiniMax-M3 发布与评测",
                "description": "聚焦 MiniMax-M3 的发布信息与实测结果。",
                "content_count": 8,
                "event_count": 2,
                "events": [
                    {
                        "id": 11,
                        "title": "MiniMax-M3 发布",
                        "summary": "模型发布与核心能力介绍。",
                        "content_count": 5,
                        "source_count": 2,
                        "editorial_tier": "important",
                    }
                ],
            }
            try:
                with patch.object(services.repository, "list_daily_topics", return_value=[topic]) as topic_query:
                    with TestClient(app) as client:
                        response = client.get("/daily-topics/42")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(topic_query.call_args.kwargs["limit"], 50)
                self.assertIn("MiniMax-M3 发布与评测", response.text)
                self.assertIn('href="/events/11?tier=important&period=24h"', response.text)
                self.assertIn("模型发布与核心能力介绍。", response.text)
            finally:
                delattr(app.state, "services")

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
                    self.assertIn("字段说明：", page.text)
                    self.assertIn('class="source-mobile-last-check"', page.text)
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

    def test_source_export_and_persistent_backups_are_downloadable(self) -> None:
        with TemporaryDirectory() as directory:
            services = build_services(build_settings(Path(directory)))
            services.repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.X_RSSHUB, locator="OpenAI"),
                "https://x.com/OpenAI?not_for_export=true",
            )
            app.state.services = services
            try:
                with TestClient(app) as client:
                    page = client.get("/sources")
                    self.assertEqual(page.status_code, 200)
                    self.assertIn("来源导出与备份", page.text)
                    self.assertIn('<details class="source-backup-details">', page.text)
                    self.assertIn('href="/sources/export.yml"', page.text)
                    self.assertIn("还没有自动备份", page.text)

                    exported = client.get("/sources/export.yml")
                    self.assertEqual(exported.status_code, 200)
                    self.assertIn("attachment", exported.headers["content-disposition"])
                    self.assertIn("sources:", exported.text)
                    self.assertIn("OpenAI", exported.text)
                    self.assertNotIn("not_for_export", exported.text)

                    backup = services.source_backups.create_backup()
                    updated_page = client.get("/sources")
                    self.assertIn(backup.filename, updated_page.text)
                    self.assertIn("每 3 天自动备份", updated_page.text)

                    downloaded = client.get(f"/sources/backups/{backup.filename}")
                    self.assertEqual(downloaded.status_code, 200)
                    self.assertIn("attachment", downloaded.headers["content-disposition"])
                    self.assertIn("OpenAI", downloaded.text)
                    self.assertEqual(client.get("/sources/backups/not-a-backup.yml").status_code, 404)
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
            related_item_id, _ = services.repository.insert_item(
                source_id,
                FeedItem(
                    guid="related",
                    title="OpenAI 新模型相关进展",
                    link="https://example.test/related",
                    content="另一条原始内容",
                    published_at=datetime.now(timezone.utc),
                ),
            )
            services.repository.save_item_summary(related_item_id, summary="相关进展摘要。")
            event_id = services.repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[item_id, related_item_id],
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
                    self.assertIn('class="mobile-primary-nav"', dashboard.text)
                    self.assertIn('class="event-preview"', dashboard.text)
                    self.assertIn(f'data-read-event-id="{event_id}"', dashboard.text)
                    self.assertIn('aria-label="不感兴趣"', dashboard.text)
                    self.assertIn("必看", dashboard.text)
                    self.assertIn("重要更新", dashboard.text)
                    self.assertIn("资讯速览", dashboard.text)
                    self.assertIn("OpenAI 新模型已开放使用", dashboard.text)
                    self.assertIn("开发者现在可以开始使用新模型", dashboard.text)
                    self.assertIn('class="event-facts"', dashboard.text)
                    self.assertIn("来源1 · 条目2", dashboard.text)
                    self.assertNotIn("全部主题", dashboard.text)
                    self.assertNotIn("重要性排序", dashboard.text)
                    self.assertIn('class="event-card-footer has-preview"', dashboard.text)
                    self.assertNotIn('aria-label="查看详情"', dashboard.text)
                    self.assertEqual(dashboard.text.count(f'href="/events/{event_id}?'), 1)

                    detail = client.get(f"/events/{event_id}?tier=must_read&period=all")
                    self.assertEqual(detail.status_code, 200)
                    self.assertIn("原始内容与来源", detail.text)
                    self.assertIn("查看原始正文", detail.text)
                    self.assertIn("中文译文", detail.text)
                    self.assertNotIn("原始标题", detail.text)
                    self.assertNotIn("OpenAI 发布新模型", detail.text)
                    self.assertIn(
                        'data-refresh-return="/?tier=must_read&amp;period=all&amp;page=1"',
                        detail.text,
                    )
                    self.assertIn("条目 2 · 来源 1", detail.text)
                    self.assertNotIn("模型解读", detail.text)
                    read_dashboard = client.get("/?tier=must_read&period=all")
                    self.assertIn('class="event-card is-read"', read_dashboard.text)

                    # 列表中展开摘要使用的异步接口继续保持可用。
                    marked = client.post(f"/events/{event_id}/read")
                    self.assertEqual(marked.status_code, 204)

                    form = client.get("/sources/new")
                    self.assertEqual(form.status_code, 200)
                    self.assertNotIn("优先级（1–10）", form.text)
                    self.assertNotIn("主题", form.text)
            finally:
                delattr(app.state, "services")

    def test_saved_events_and_global_fetch_policy_work_together(self) -> None:
        with TemporaryDirectory() as directory:
            services = build_services(build_settings(Path(directory)))
            source_id = services.repository.create_source(
                SourceDraft(name="OpenAI", kind=SourceKind.RSS, locator="https://example.test/feed"),
                "https://example.test/feed",
            )
            item_id, _ = services.repository.insert_item(
                source_id,
                FeedItem(
                    guid="saved-event",
                    title="值得稍后阅读的更新",
                    link="https://example.test/saved-event",
                    content="原始内容",
                    published_at=datetime.now(timezone.utc),
                    media=[
                        {
                            "kind": "image",
                            "url": "https://cdn.example.test/preview.jpg",
                            "alt": "预览图",
                        }
                    ],
                ),
            )
            services.repository.save_item_summary(item_id, summary="可稍后阅读的摘要")
            event_id = services.repository.apply_curation_groups(
                [
                    CurationGroup(
                        item_ids=[item_id],
                        primary_item_id=item_id,
                        tier=EditorialTier.MUST_READ,
                        reason="测试收藏",
                        order=1,
                    )
                ]
            )[0]
            app.state.services = services
            try:
                with TestClient(app) as client:
                    settings = client.get("/settings")
                    self.assertEqual(settings.status_code, 200)
                    self.assertIn("统一抓取策略", settings.text)
                    self.assertIn('name="interval_minutes"', settings.text)
                    self.assertIn("今日热点刷新", settings.text)
                    self.assertIn('action="/settings/daily-topics-interval"', settings.text)
                    self.assertIn("通知统计范围", settings.text)
                    self.assertIn('name="window_hours"', settings.text)

                    policy = client.post(
                        "/settings/fetch-policy",
                        data={"interval_minutes": "30"},
                        follow_redirects=False,
                    )
                    self.assertEqual(policy.status_code, 303)
                    self.assertIn("#fetch", policy.headers["location"])
                    self.assertEqual(services.repository.get_fetch_policy().interval_minutes, 30)

                    daily_topics_interval = client.post(
                        "/settings/daily-topics-interval",
                        data={"interval_minutes": "45"},
                        follow_redirects=False,
                    )
                    self.assertEqual(daily_topics_interval.status_code, 303)
                    self.assertIn("#daily-topics", daily_topics_interval.headers["location"])
                    self.assertEqual(
                        services.repository.get_daily_topic_refresh_interval_minutes(), 45
                    )

                    push_window = client.post(
                        "/settings/web-push-window",
                        data={"window_hours": "4"},
                        follow_redirects=False,
                    )
                    self.assertEqual(push_window.status_code, 303)
                    self.assertIn("#push", push_window.headers["location"])
                    self.assertEqual(services.repository.get_web_push_window_hours(), 4)

                    form = client.get("/sources/new")
                    self.assertNotIn('name="poll_interval_minutes"', form.text)
                    self.assertIn("抓取由全局策略管理", form.text)

                    dashboard = client.get("/?tier=must_read&period=all")
                    self.assertIn('aria-label="收藏"', dashboard.text)
                    saved = client.post(
                        f"/events/{event_id}/save",
                        data={"origin": "dashboard", "tier": "must_read", "period": "all", "page": "1"},
                        follow_redirects=False,
                    )
                    self.assertEqual(saved.status_code, 303)
                    self.assertEqual(urlparse(saved.headers["location"]).path, "/")

                    # 收藏后即使来源暂停，也必须从收藏页进入详情。
                    services.repository.update_source(source_id, {"enabled": 0})
                    saved_page = client.get("/saved")
                    self.assertIn("值得稍后阅读的更新", saved_page.text)
                    self.assertIn('class="event-facts"', saved_page.text)
                    self.assertIn('class="event-card-footer has-preview"', saved_page.text)
                    self.assertIn(f'data-read-event-id="{event_id}"', saved_page.text)
                    self.assertIn('aria-label="取消收藏"', saved_page.text)
                    self.assertNotIn('aria-label="查看详情"', saved_page.text)
                    self.assertNotIn('saved-event-summary', saved_page.text)
                    detail = client.get(f"/events/{event_id}?origin=saved")
                    self.assertEqual(detail.status_code, 200)
                    self.assertIn('href="/saved?page=1"', detail.text)
                    self.assertIn("媒体预览", detail.text)
                    self.assertIn("https://cdn.example.test/preview.jpg", detail.text)

                    unsaved = client.post(
                        f"/events/{event_id}/unsave",
                        data={"origin": "saved", "page": "1"},
                        follow_redirects=False,
                    )
                    self.assertEqual(urlparse(unsaved.headers["location"]).path, "/saved")
                    self.assertFalse(services.repository.is_event_saved(event_id))
            finally:
                delattr(app.state, "services")
