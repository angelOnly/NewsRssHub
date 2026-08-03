from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import load_user_profile
from app.domain.models import SourceDraft, SourceKind
from app.runtime import ApplicationServices, build_services
from app.services.connections import ConnectionRequiredError
from app.services.llm_connection import LLMConnectionError
from app.services.x_session import XSessionError


def _clean_label(value: str) -> str:
    return re.sub(r"^[^\w\u4e00-\u9fff]+\s*", "", value).strip()


def _fmt_time(value: str | None) -> str:
    if not value:
        return "刚刚"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
        seconds = max(0, int(delta.total_seconds()))
        if seconds < 60:
            return "刚刚"
        if seconds < 3600:
            return f"{seconds // 60} 分钟前"
        if seconds < 86400:
            return f"{seconds // 3600} 小时前"
        return f"{seconds // 86400} 天前"
    except (TypeError, ValueError):
        return value[:16]


def _importance_label(score: float) -> str:
    if score >= 75:
        return "关键"
    if score >= 55:
        return "重要"
    if score >= 35:
        return "值得关注"
    return "更新"


def _kind_label(kind: str) -> str:
    return {
        "rss": "RSS",
        "x_rsshub": "X 账号（会话）",
        "reddit": "Reddit",
    }.get(kind, kind)


@asynccontextmanager
async def lifespan(app: FastAPI):
    services = build_services()
    services.pipeline.bootstrap()
    app.state.services = services
    yield


app = FastAPI(title="NewsRSSHub", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["relative_time"] = _fmt_time
templates.env.filters["importance_label"] = _importance_label
templates.env.filters["kind_label"] = _kind_label


def get_services(request: Request) -> ApplicationServices:
    return request.app.state.services


def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200) -> HTMLResponse:
    services = get_services(request)
    base = {
        "request": request,
        "active_path": request.url.path,
        "x_credential": services.x_sessions.status(),
        "llm_credential": services.llm_connections.status(),
        "platform_connections": services.connections.source_connections(),
        "has_x_sources": services.repository.has_enabled_source_kind("x_rsshub"),
    }
    if context:
        base.update(context)
    return templates.TemplateResponse(request=request, name=name, context=base, status_code=status_code)


def sources_redirect(*, notice: str = "", error: str = "") -> RedirectResponse:
    query = urlencode({key: value for key, value in {"notice": notice, "error": error}.items() if value})
    return RedirectResponse(f"/sources?{query}" if query else "/sources", status_code=303)


def dashboard_redirect(*, period: str = "24h", topic: str = "", page: int = 1, notice: str = "") -> RedirectResponse:
    query = urlencode(
        {
            "period": period,
            "topic": topic,
            "page": max(1, page),
            **({"notice": notice} if notice else {}),
        }
    )
    return RedirectResponse(f"/?{query}", status_code=303)


def settings_redirect(*, anchor: str = "", notice: str = "", error: str = "") -> RedirectResponse:
    query = urlencode({key: value for key, value in {"notice": notice, "error": error}.items() if value})
    target = f"/settings?{query}" if query else "/settings"
    if anchor:
        target = f"{target}#{anchor}"
    return RedirectResponse(target, status_code=303)


def x_session_redirect(*, notice: str = "", error: str = "") -> RedirectResponse:
    return settings_redirect(anchor="x", notice=notice, error=error)


def llm_settings_redirect(*, notice: str = "", error: str = "") -> RedirectResponse:
    return settings_redirect(anchor="model", notice=notice, error=error)


@app.get("/health")
def health(request: Request) -> JSONResponse:
    services = get_services(request)
    return JSONResponse(
        {
            "status": "ok",
            "x_session": services.x_sessions.status().state,
            "llm_connection": services.llm_connections.status().state,
        }
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    period: str = "24h",
    topic: str = "",
    page: int = 1,
) -> HTMLResponse:
    services = get_services(request)
    page = max(1, page)
    events = services.repository.list_events(period=period, topic=topic, limit=50, offset=(page - 1) * 50)
    total_events = services.repository.count_events(period=period, topic=topic)
    profile = load_user_profile(services.settings)
    topics = [_clean_label(str(interest.get("name", ""))) for interest in profile.get("interests", []) if isinstance(interest, dict)]
    return render(
        request,
        "dashboard.html",
        {
            "events": events,
            "stats": services.repository.dashboard_stats(),
            "period": period,
            "topic": topic,
            "topics": [item for item in topics if item],
            "page": page,
            "has_more": page * 50 < total_events,
        },
    )


@app.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: int) -> HTMLResponse:
    event = get_services(request).repository.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="未找到该事件")
    return render(request, "event_detail.html", {"event": event, "analysis": (event.get("analysis") or {}).get("payload", {})})


@app.post("/events/{event_id}/not-interested")
def mark_event_not_interested(
    request: Request,
    event_id: int,
    period: str = Form("24h"),
    topic: str = Form(""),
    page: int = Form(1),
) -> RedirectResponse:
    repository = get_services(request).repository
    if not repository.get_event(event_id):
        raise HTTPException(status_code=404, detail="未找到该事件")
    repository.mark_event_not_interested(event_id)
    return dashboard_redirect(period=period, topic=topic, page=page, notice="已隐藏这条内容。")


@app.get("/briefs", response_class=HTMLResponse)
def briefs(request: Request) -> HTMLResponse:
    return render(request, "briefs.html", {"briefs": get_services(request).repository.list_briefs()})


@app.get("/briefs/{brief_date}", response_class=HTMLResponse)
def brief_detail(request: Request, brief_date: str) -> HTMLResponse:
    repository = get_services(request).repository
    brief = repository.get_brief(brief_date)
    if not brief:
        raise HTTPException(status_code=404, detail="未找到该日报")
    return render(request, "brief_detail.html", {"brief": brief, "events": repository.get_events_by_ids(brief.get("event_ids", []))})


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request, notice: str = "", error: str = "") -> HTMLResponse:
    services = get_services(request)
    return render(
        request,
        "sources.html",
        {"sources": services.repository.list_sources(), "notice": notice, "error": error},
    )


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, notice: str = "", error: str = "") -> HTMLResponse:
    return render(request, "settings.html", {"notice": notice, "error": error})


@app.get("/connections")
def connections_redirect(request: Request, notice: str = "", error: str = "") -> RedirectResponse:
    """Keep the previous platform-connection URL usable after the UI merge."""

    return settings_redirect(anchor="platforms", notice=notice, error=error)


@app.get("/settings/x-session")
def x_session_settings_redirect(request: Request, notice: str = "", error: str = "") -> RedirectResponse:
    """Keep old X-cookie links and bookmarks working."""

    return settings_redirect(anchor="x", notice=notice, error=error)


@app.post("/settings/x-session")
def save_x_session(request: Request, cookie_value: str = Form(...)) -> RedirectResponse:
    try:
        services = get_services(request)
        status = services.x_sessions.save_from_web(cookie_value)
        retried = services.sources.requeue_failed_platform_sources(SourceKind.X_RSSHUB)
        suffix = f"已安排 {retried} 个此前失败的 X 来源重新抓取。" if retried else ""
        return x_session_redirect(notice=f"X 登录 Cookie 已验证并更新（指纹 {status.fingerprint}）。{suffix}")
    except XSessionError as exc:
        return x_session_redirect(error=str(exc))
    except Exception:
        return x_session_redirect(error="保存 X 登录 Cookie 时发生异常，请稍后重试。")


@app.post("/settings/x-session/test")
def test_x_session(request: Request) -> RedirectResponse:
    try:
        services = get_services(request)
        status = services.x_sessions.test_saved()
        retried = services.sources.requeue_failed_platform_sources(SourceKind.X_RSSHUB)
        suffix = f"已安排 {retried} 个此前失败的 X 来源重新抓取。" if retried else ""
        return x_session_redirect(notice=f"X 登录 Cookie 当前可用（指纹 {status.fingerprint}）。{suffix}")
    except XSessionError as exc:
        return x_session_redirect(error=str(exc))
    except Exception:
        return x_session_redirect(error="暂时无法验证 X 登录 Cookie，请稍后重试。")


@app.get("/settings/llm")
def llm_settings_redirect_legacy(request: Request, notice: str = "", error: str = "") -> RedirectResponse:
    """Keep the previous model-settings URL usable after the UI merge."""

    return settings_redirect(anchor="model", notice=notice, error=error)


@app.post("/settings/llm")
def save_llm_settings(
    request: Request,
    api_key_value: str = Form(""),
    base_url: str = Form(...),
    model_name: str = Form(""),
    enabled: bool = Form(False),
) -> RedirectResponse:
    try:
        status = get_services(request).llm_connections.save_from_web(
            api_key_value=api_key_value,
            base_url=base_url,
            model_name=model_name,
            enabled=enabled,
        )
        suffix = f"（指纹 {status.fingerprint}）" if status.fingerprint else ""
        return llm_settings_redirect(notice=f"模型连接已测试并保存{suffix}。")
    except LLMConnectionError as exc:
        return llm_settings_redirect(error=str(exc))
    except Exception:
        return llm_settings_redirect(error="保存模型连接时发生异常，请稍后重试。")


@app.post("/settings/llm/test")
def test_llm_settings(request: Request) -> RedirectResponse:
    try:
        status = get_services(request).llm_connections.test_saved()
        return llm_settings_redirect(notice=f"模型服务连接正常，正在使用 {status.model_name}。")
    except LLMConnectionError as exc:
        return llm_settings_redirect(error=str(exc))
    except Exception:
        return llm_settings_redirect(error="暂时无法测试模型服务，请稍后重试。")


@app.get("/sources/new", response_class=HTMLResponse)
def new_source_form(request: Request, error: str = "", connection: str = "") -> HTMLResponse:
    services = get_services(request)
    required_connection = next(
        (item for item in services.connections.source_connections() if item.key == connection),
        None,
    )
    return render(
        request,
        "source_form.html",
        {
            "source": None,
            "choices": services.sources.form_choices(),
            "connections": services.connections.source_connections(),
            "required_connection": required_connection,
            "error": error,
            "mode": "new",
        },
    )


def _make_draft(
    *,
    name: str,
    kind: str,
    locator: str,
    category: str,
    priority: int,
    is_official: bool,
    poll_interval_minutes: int,
    fallback_url: str,
    enabled: bool,
) -> SourceDraft:
    try:
        source_kind = SourceKind(kind)
    except ValueError as exc:
        raise ValueError("请选择有效的来源类型。") from exc
    if not name.strip() or not locator.strip():
        raise ValueError("请填写来源名称和地址。")
    return SourceDraft(
        name=name.strip()[:120],
        kind=source_kind,
        locator=locator.strip(),
        category=category.strip()[:80] or "未分类",
        priority=max(1, min(int(priority), 10)),
        is_official=is_official,
        poll_interval_minutes=max(5, min(int(poll_interval_minutes), 1440)),
        fallback_url=fallback_url.strip()[:500],
        enabled=enabled,
    )


@app.post("/sources/new")
def create_source(
    request: Request,
    name: str = Form(...),
    kind: str = Form(...),
    locator: str = Form(...),
    category: str = Form("未分类"),
    priority: int = Form(5),
    is_official: bool = Form(False),
    poll_interval_minutes: int = Form(60),
    fallback_url: str = Form(""),
    enabled: bool = Form(False),
) -> RedirectResponse:
    services = get_services(request)
    try:
        if kind == "auto":
            kind = services.sources.detect_kind(locator).value
        draft = _make_draft(
            name=name, kind=kind, locator=locator, category=category, priority=priority,
            is_official=is_official, poll_interval_minutes=poll_interval_minutes,
            fallback_url=fallback_url, enabled=enabled,
        )
        source, validation = services.sources.add_source(draft, validate=True)
        message = validation.message if validation else "来源已添加。"
        prefix = "已添加：" if validation and validation.ok else "已保存，但需要检查："
        return sources_redirect(notice=f"{prefix}{message}")
    except ConnectionRequiredError as exc:
        return RedirectResponse(
            f"/sources/new?{urlencode({'error': str(exc), 'connection': exc.connection.key})}",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(f"/sources/new?{urlencode({'error': str(exc)})}", status_code=303)


@app.get("/sources/{source_id}/edit", response_class=HTMLResponse)
def edit_source_form(request: Request, source_id: int, error: str = "") -> HTMLResponse:
    services = get_services(request)
    source = services.repository.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="未找到来源")
    return render(
        request,
        "source_form.html",
        {"source": source, "choices": services.sources.form_choices(), "error": error, "mode": "edit"},
    )


@app.post("/sources/{source_id}/edit")
def edit_source(
    request: Request,
    source_id: int,
    name: str = Form(...),
    category: str = Form("未分类"),
    priority: int = Form(5),
    is_official: bool = Form(False),
    poll_interval_minutes: int = Form(60),
    fallback_url: str = Form(""),
    enabled: bool = Form(False),
) -> RedirectResponse:
    services = get_services(request)
    source = services.repository.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="未找到来源")
    try:
        draft = _make_draft(
            name=name, kind=source["kind"], locator=source["locator"], category=category,
            priority=priority, is_official=is_official, poll_interval_minutes=poll_interval_minutes,
            fallback_url=fallback_url, enabled=enabled,
        )
        services.sources.update_source(source_id, draft)
        return sources_redirect(notice="来源配置已保存。")
    except Exception as exc:
        return RedirectResponse(f"/sources/{source_id}/edit?{urlencode({'error': str(exc)})}", status_code=303)


@app.post("/sources/{source_id}/test")
def test_source(request: Request, source_id: int) -> RedirectResponse:
    try:
        result = get_services(request).sources.validate_source(source_id)
        prefix = "连接正常：" if result.ok else "连接失败："
        return sources_redirect(notice=f"{prefix}{result.message}")
    except Exception as exc:
        return sources_redirect(error=str(exc))


@app.post("/sources/{source_id}/toggle")
def toggle_source(request: Request, source_id: int) -> RedirectResponse:
    repository = get_services(request).repository
    source = repository.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="未找到来源")
    repository.update_source(source_id, {"enabled": int(not source["enabled"])})
    if source["enabled"]:
        return sources_redirect(notice="来源已暂停；刷新首页或简报后，会立即隐藏它已有的内容。")
    return sources_redirect(notice="来源已启用，将按设定频率恢复抓取。")


@app.post("/sources/{source_id}/archive")
def archive_source(request: Request, source_id: int) -> RedirectResponse:
    get_services(request).repository.archive_source(source_id)
    return sources_redirect(notice="来源已归档，可随时从数据库恢复。")
