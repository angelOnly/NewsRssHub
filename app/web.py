from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.domain.curation import EditorialTier
from app.domain.models import SourceDraft, SourceKind
from app.runtime import ApplicationServices, build_services
from app.services.connections import ConnectionRequiredError
from app.services.llm_client import LLMRequestError
from app.services.llm_connection import LLMConnectionError
from app.services.x_session import XSessionError
from app.services.web_push import WebPushError, WebPushSubscriptionError


TIER_TABS: tuple[tuple[str, str], ...] = (
    (EditorialTier.MUST_READ.value, "必看"),
    (EditorialTier.IMPORTANT.value, "重要更新"),
    (EditorialTier.BRIEF.value, "资讯速览"),
    (EditorialTier.HIDDEN.value, "已隐藏"),
)

SOURCE_PAGE_SIZE = 20
SOURCE_PLATFORM_TABS: tuple[tuple[str, str], ...] = (
    ("all", "全部"),
    (SourceKind.X_RSSHUB.value, "X"),
    (SourceKind.REDDIT.value, "Reddit"),
    (SourceKind.YOUTUBE.value, "YouTube"),
    (SourceKind.RSS.value, "RSS"),
)


def _fmt_time(value: str | None) -> str:
    if not value:
        return "刚刚"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
        seconds = int(delta.total_seconds())
        if seconds < 0:
            seconds = -seconds
            if seconds < 60:
                return "不到 1 分钟后"
            if seconds < 3600:
                return f"{seconds // 60} 分钟后"
            if seconds < 86400:
                return f"{seconds // 3600} 小时后"
            return f"{seconds // 86400} 天后"
        if seconds < 60:
            return "刚刚"
        if seconds < 3600:
            return f"{seconds // 60} 分钟前"
        if seconds < 86400:
            return f"{seconds // 3600} 小时前"
        return f"{seconds // 86400} 天前"
    except (TypeError, ValueError):
        return value[:16]


def _compact_relative_time(value: str | None) -> str:
    """返回来源移动端使用的紧凑时间，前后关系由字段名表达。"""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delta_seconds = int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
        seconds = abs(delta_seconds)
        if seconds < 60:
            return "<1 min"
        if seconds < 3600:
            minutes = (seconds + 59) // 60 if delta_seconds < 0 else seconds // 60
            return f"{minutes} min"
        if seconds < 86400:
            hours = (seconds + 3599) // 3600 if delta_seconds < 0 else seconds // 3600
            return f"{hours} h"
        days = (seconds + 86399) // 86400 if delta_seconds < 0 else seconds // 86400
        return f"{days} d"
    except (TypeError, ValueError):
        return "—"


def _kind_label(kind: str) -> str:
    return {
        "rss": "RSS",
        "x_rsshub": "X 账号（会话）",
        "reddit": "Reddit",
        "youtube": "YouTube",
    }.get(kind, kind)


def _tier_label(tier: str) -> str:
    return dict(TIER_TABS).get(tier, "待筛选")


def _safe_tier(value: str) -> EditorialTier:
    try:
        tier = EditorialTier(value)
    except ValueError:
        return EditorialTier.MUST_READ
    return tier if tier != EditorialTier.PENDING else EditorialTier.MUST_READ


def _safe_source_kind(value: str) -> str:
    if value == "all":
        return "all"
    try:
        return SourceKind(value).value
    except ValueError:
        return "all"


def _source_page_value(value: int | str) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "services"):
        services = build_services()
        services.pipeline.bootstrap()
        app.state.services = services
    yield


app = FastAPI(title="NewsRSSHub", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["relative_time"] = _fmt_time
templates.env.filters["compact_relative_time"] = _compact_relative_time
templates.env.filters["kind_label"] = _kind_label
templates.env.filters["tier_label"] = _tier_label


def get_services(request: Request) -> ApplicationServices:
    return request.app.state.services


def _require_push_request(request: Request) -> None:
    """Push 写接口只接受页面脚本发起的 JSON 请求，拦住跨站表单提交。"""

    if request.headers.get("X-NewsRSSHub-Push") != "1":
        raise HTTPException(status_code=403, detail="通知请求来源无效。")


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    services = get_services(request)
    base = {
        "request": request,
        "active_path": request.url.path,
        "x_credential": services.x_sessions.status(),
        "llm_credential": services.llm_connections.status(),
        "platform_connections": services.connections.source_connections(),
        "has_x_sources": services.repository.has_enabled_source_kind("x_rsshub"),
        "skill_status": services.pipeline.skill_loader.status(),
    }
    if context:
        base.update(context)
    return templates.TemplateResponse(request=request, name=name, context=base, status_code=status_code)


def sources_redirect(
    *,
    notice: str = "",
    notice_level: str = "success",
    error: str = "",
    kind: str = "all",
    page: int | str = 1,
) -> RedirectResponse:
    selected_kind = _safe_source_kind(kind)
    selected_page = _source_page_value(page)
    values: dict[str, str | int] = {
        **({"kind": selected_kind} if selected_kind != "all" else {}),
        **({"page": selected_page} if selected_page > 1 else {}),
        **({"notice": notice} if notice else {}),
        **({"notice_level": notice_level} if notice else {}),
        **({"error": error} if error else {}),
    }
    query = urlencode(values)
    return RedirectResponse(f"/sources?{query}" if query else "/sources", status_code=303)


def dashboard_redirect(
    *,
    tier: str = EditorialTier.MUST_READ.value,
    period: str = "24h",
    page: int = 1,
    notice: str = "",
) -> RedirectResponse:
    query = urlencode(
        {
            "tier": _safe_tier(tier).value,
            "period": period if period in {"24h", "7d", "30d", "all"} else "24h",
            "page": max(1, page),
            **({"notice": notice} if notice else {}),
        }
    )
    return RedirectResponse(f"/?{query}", status_code=303)


def saved_redirect(*, page: int = 1, notice: str = "") -> RedirectResponse:
    query = urlencode(
        {
            "page": max(1, page),
            **({"notice": notice} if notice else {}),
        }
    )
    return RedirectResponse(f"/saved?{query}", status_code=303)


def event_detail_redirect(
    event_id: int,
    *,
    tier: str = EditorialTier.MUST_READ.value,
    period: str = "24h",
    page: int = 1,
    origin: str = "",
    notice: str = "",
    error: str = "",
) -> RedirectResponse:
    query = urlencode(
        {
            "tier": _safe_tier(tier).value,
            "period": period if period in {"24h", "7d", "30d", "all"} else "24h",
            "page": max(1, page),
            **({"origin": "saved"} if origin == "saved" else {}),
            **({"notice": notice} if notice else {}),
            **({"error": error} if error else {}),
        }
    )
    return RedirectResponse(f"/events/{event_id}?{query}", status_code=303)


def feedback_redirect(
    event_id: int,
    *,
    origin: str,
    tier: str,
    period: str,
    page: int,
    notice: str,
) -> RedirectResponse:
    if origin == "saved":
        return saved_redirect(page=page, notice=notice)
    if origin == "detail":
        return event_detail_redirect(
            event_id, tier=tier, period=period, page=page, notice=notice
        )
    return dashboard_redirect(tier=tier, period=period, page=page, notice=notice)


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


def fetch_settings_redirect(*, notice: str = "", error: str = "") -> RedirectResponse:
    return settings_redirect(anchor="fetch", notice=notice, error=error)


def push_settings_redirect(*, notice: str = "", error: str = "") -> RedirectResponse:
    return settings_redirect(anchor="push", notice=notice, error=error)


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest() -> FileResponse:
    return FileResponse(
        Path("app/static/manifest.webmanifest"),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    # 根路径的 Worker 才能控制首页和资讯详情页；禁止长缓存以便发布后及时更新。
    return FileResponse(
        Path("app/static/sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/push/config")
def web_push_config(request: Request) -> JSONResponse:
    services = get_services(request)
    try:
        payload = services.web_push.public_config()
    except WebPushError as exc:
        payload = {
            "available": False,
            "message": str(exc),
            "status": asdict(services.web_push.status()),
        }
    except Exception:
        logging.getLogger(__name__).exception("读取 Web Push 配置失败")
        payload = {
            "available": False,
            "message": "手机通知暂不可用，请稍后重试。",
            "status": asdict(services.web_push.status()),
        }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/api/push/subscription")
async def save_web_push_subscription(request: Request) -> JSONResponse:
    _require_push_request(request)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise WebPushSubscriptionError("手机通知订阅格式无效，请重新开启通知。")
        status = get_services(request).web_push.save_subscription(payload)
        return JSONResponse({"status": asdict(status)}, headers={"Cache-Control": "no-store"})
    except WebPushError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logging.getLogger(__name__).exception("保存 Web Push 订阅失败")
        raise HTTPException(status_code=500, detail="保存手机通知失败，请稍后重试。") from exc


@app.delete("/api/push/subscription")
def remove_web_push_subscription(request: Request) -> Response:
    _require_push_request(request)
    get_services(request).web_push.unsubscribe()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@app.post("/api/push/test")
def test_web_push(request: Request) -> JSONResponse:
    _require_push_request(request)
    try:
        get_services(request).web_push.send_test()
        return JSONResponse({"ok": True, "message": "测试通知已发送。"})
    except WebPushSubscriptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WebPushError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logging.getLogger(__name__).exception("发送 Web Push 测试通知失败")
        raise HTTPException(status_code=503, detail="测试通知发送失败，请稍后重试。") from exc


@app.get("/health")
def health(request: Request) -> JSONResponse:
    services = get_services(request)
    stats = services.repository.dashboard_stats()
    return JSONResponse(
        {
            "status": "ok",
            "database": "ok",
            "x_session": services.x_sessions.status().state,
            "llm_connection": services.llm_connections.status().state,
            "web_push": services.web_push.status().state,
            "curation_skill": "available" if services.pipeline.skill_loader.status().available else "unavailable",
            "pending_summary": stats["pending_summary"],
            "pending_curation": stats["pending_curation"],
            "pending_translation": stats["pending_translation"],
        }
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    tier: str = EditorialTier.MUST_READ.value,
    period: str = "24h",
    page: int = 1,
) -> HTMLResponse:
    services = get_services(request)
    selected_tier = _safe_tier(tier)
    period = period if period in {"24h", "7d", "30d", "all"} else "24h"
    page = max(1, page)
    events = services.repository.list_events(
        tier=selected_tier, period=period, limit=50, offset=(page - 1) * 50
    )
    total_events = services.repository.count_events(tier=selected_tier, period=period)
    return render(
        request,
        "dashboard.html",
        {
            "events": events,
            "stats": services.repository.dashboard_stats(),
            "tier_counts": services.repository.tier_counts(period),
            "tier_tabs": TIER_TABS,
            "tier": selected_tier.value,
            "period": period,
            "page": page,
            "has_more": page * 50 < total_events,
        },
    )


@app.get("/saved", response_class=HTMLResponse)
def saved_events(request: Request, page: int = 1, notice: str = "") -> HTMLResponse:
    repository = get_services(request).repository
    page = max(1, page)
    events = repository.list_saved_events(limit=50, offset=(page - 1) * 50)
    total_events = repository.count_saved_events()
    return render(
        request,
        "saved.html",
        {
            "events": events,
            "page": page,
            "notice": notice,
            "has_more": page * 50 < total_events,
        },
    )


@app.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(
    request: Request,
    event_id: int,
    tier: str = EditorialTier.MUST_READ.value,
    period: str = "24h",
    page: int = 1,
    origin: str = "",
    notice: str = "",
    error: str = "",
) -> HTMLResponse:
    repository = get_services(request).repository
    event = repository.get_event(event_id)
    if not event and repository.is_event_saved(event_id):
        # 收藏是独立的保留承诺，来源后来停用或归档也必须仍可阅读。
        event = repository.get_event(event_id, include_inactive_sources=True)
    if not event:
        raise HTTPException(status_code=404, detail="未找到该事件")
    # 成功进入详情页是明确的主动阅读行为；列表摘要展开仍通过异步接口记录。
    repository.mark_event_read(event_id)
    return_query = urlencode(
        {"tier": _safe_tier(tier).value, "period": period, "page": max(1, page)}
    )
    from_saved = origin == "saved"
    return render(
        request,
        "event_detail.html",
        {
            "event": event,
            "return_query": return_query,
            "return_url": f"/saved?page={max(1, page)}" if from_saved else f"/?{return_query}",
            "return_label": "返回收藏" if from_saved else "返回热点列表",
            "action_origin": "saved" if from_saved else "detail",
            "return_tier": _safe_tier(tier).value,
            "return_period": period if period in {"24h", "7d", "30d", "all"} else "24h",
            "return_page": max(1, page),
            "notice": notice,
            "error": error,
        },
    )


@app.post("/events/{event_id}/items/{item_id}/translate")
def translate_event_item(
    request: Request,
    event_id: int,
    item_id: int,
    tier: str = Form(EditorialTier.MUST_READ.value),
    period: str = Form("24h"),
    page: int = Form(1),
    origin: str = Form(""),
) -> RedirectResponse:
    services = get_services(request)
    event = services.repository.get_event(event_id)
    if not event and services.repository.is_event_saved(event_id):
        event = services.repository.get_event(event_id, include_inactive_sources=True)
    if not event:
        raise HTTPException(status_code=404, detail="未找到该事件")
    if item_id not in {int(item["id"]) for item in event["items"]}:
        raise HTTPException(status_code=404, detail="未找到该事件中的来源内容")
    try:
        outcome = services.translator.translate_item(item_id)
        notice = {
            "cached": "中文译文已存在。",
            "direct": "正文已经是中文，已保存为中文正文。",
            "model": "中文译文已生成。",
        }.get(outcome, "中文译文已生成。")
        return event_detail_redirect(
            event_id, tier=tier, period=period, page=page, origin=origin, notice=notice
        )
    except LLMRequestError as exc:
        return event_detail_redirect(
            event_id, tier=tier, period=period, page=page, origin=origin, error=str(exc)
        )
    except Exception:
        return event_detail_redirect(
            event_id,
            tier=tier,
            period=period,
            page=page,
            origin=origin,
            error="正文翻译暂时失败，请稍后重试。",
        )


@app.post("/events/{event_id}/not-interested")
def mark_event_not_interested(
    request: Request,
    event_id: int,
    tier: str = Form(EditorialTier.MUST_READ.value),
    period: str = Form("24h"),
    page: int = Form(1),
    origin: str = Form(""),
) -> RedirectResponse:
    repository = get_services(request).repository
    if not repository.get_event(event_id, include_inactive_sources=origin == "saved"):
        raise HTTPException(status_code=404, detail="未找到该事件")
    repository.mark_event_not_interested(event_id)
    if origin == "saved":
        return saved_redirect(page=page, notice="已隐藏这条内容；收藏仍会保留。")
    return dashboard_redirect(tier=tier, period=period, page=page, notice="已隐藏这条内容。")


@app.post("/events/{event_id}/save")
def save_event(
    request: Request,
    event_id: int,
    origin: str = Form("dashboard"),
    tier: str = Form(EditorialTier.MUST_READ.value),
    period: str = Form("24h"),
    page: int = Form(1),
) -> RedirectResponse:
    repository = get_services(request).repository
    if not repository.get_event(event_id, include_inactive_sources=True):
        raise HTTPException(status_code=404, detail="未找到该事件")
    repository.save_event(event_id)
    return feedback_redirect(
        event_id,
        origin=origin,
        tier=tier,
        period=period,
        page=page,
        notice="已收藏，可在收藏页稍后阅读。",
    )


@app.post("/events/{event_id}/unsave")
def unsave_event(
    request: Request,
    event_id: int,
    origin: str = Form("dashboard"),
    tier: str = Form(EditorialTier.MUST_READ.value),
    period: str = Form("24h"),
    page: int = Form(1),
) -> RedirectResponse:
    repository = get_services(request).repository
    if not repository.get_event(event_id, include_inactive_sources=True):
        raise HTTPException(status_code=404, detail="未找到该事件")
    repository.unsave_event(event_id)
    return feedback_redirect(
        event_id,
        origin=origin,
        tier=tier,
        period=period,
        page=page,
        notice="已取消收藏。",
    )


@app.post("/events/{event_id}/read", status_code=204)
def mark_event_read(request: Request, event_id: int) -> Response:
    """摘要展开后异步记录已读，不改变当前列表的位置。"""

    repository = get_services(request).repository
    if not repository.get_event(event_id, include_inactive_sources=True):
        raise HTTPException(status_code=404, detail="未找到该事件")
    repository.mark_event_read(event_id)
    return Response(status_code=204)


@app.post("/events/{event_id}/restore")
def restore_event(
    request: Request,
    event_id: int,
    period: str = Form("24h"),
    page: int = Form(1),
) -> RedirectResponse:
    repository = get_services(request).repository
    if not repository.get_event(event_id):
        raise HTTPException(status_code=404, detail="未找到该事件")
    repository.restore_event(event_id)
    return dashboard_redirect(
        tier=EditorialTier.HIDDEN.value, period=period, page=page, notice="已恢复你的隐藏设置。"
    )


@app.get("/briefs", response_class=HTMLResponse)
def briefs(request: Request) -> HTMLResponse:
    return render(request, "briefs.html", {"briefs": get_services(request).repository.list_briefs()})


@app.get("/briefs/{brief_date}", response_class=HTMLResponse)
def brief_detail(request: Request, brief_date: str) -> HTMLResponse:
    repository = get_services(request).repository
    brief = repository.get_brief(brief_date)
    if not brief:
        raise HTTPException(status_code=404, detail="未找到该日报")
    return render(
        request,
        "brief_detail.html",
        {"brief": brief, "events": repository.get_events_by_ids(brief.get("event_ids", []))},
    )


@app.get("/sources", response_class=HTMLResponse)
def sources(
    request: Request,
    kind: str = "all",
    page: int = 1,
    notice: str = "",
    notice_level: str = "success",
    error: str = "",
) -> HTMLResponse:
    services = get_services(request)
    selected_kind = _safe_source_kind(kind)
    source_page = services.repository.list_sources_page(
        kind=None if selected_kind == "all" else selected_kind,
        page=_source_page_value(page),
        page_size=SOURCE_PAGE_SIZE,
    )
    current_page_testable_count = sum(1 for source in source_page.sources if source["enabled"])
    can_queue_current_page_test = (
        selected_kind != "all"
        and current_page_testable_count > 0
        and services.connections.for_kind(selected_kind).usable
    )
    kind_counts = services.repository.source_kind_counts()
    platform_tabs = [
        {
            "kind": tab_kind,
            "label": label,
            "count": source_page.total if tab_kind == "all" else kind_counts.get(tab_kind, 0),
        }
        for tab_kind, label in SOURCE_PLATFORM_TABS
    ]
    return render(
        request,
        "sources.html",
        {
            "sources": source_page.sources,
            "source_total": source_page.total,
            "source_page": source_page.page,
            "source_page_count": source_page.page_count,
            "source_page_size": source_page.page_size,
            "source_page_start": (source_page.page - 1) * source_page.page_size + 1
            if source_page.total
            else 0,
            "source_page_end": min(source_page.page * source_page.page_size, source_page.total),
            "selected_source_kind": selected_kind,
            "current_page_testable_count": current_page_testable_count,
            "can_queue_current_page_test": can_queue_current_page_test,
            "platform_tabs": platform_tabs,
            "source_backups": services.source_backups.list_backups(),
            "fetch_policy": services.repository.get_fetch_policy(),
            "notice": notice,
            "notice_level": notice_level if notice_level in {"success", "warning"} else "success",
            "error": error,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, notice: str = "", error: str = "") -> HTMLResponse:
    services = get_services(request)
    return render(
        request,
        "settings.html",
        {
            "notice": notice,
            "error": error,
            "fetch_policy": services.repository.get_fetch_policy(),
            "web_push_window_hours": services.repository.get_web_push_window_hours(),
        },
    )


@app.post("/settings/fetch-policy")
def save_fetch_policy(
    request: Request,
    interval_minutes: int = Form(...),
) -> RedirectResponse:
    try:
        policy, rescheduled = get_services(request).repository.save_fetch_policy(interval_minutes)
        return fetch_settings_redirect(
            notice=(
                f"已将全部来源统一设为每 {policy.interval_minutes} 分钟抓取一次；"
                f"{rescheduled} 个启用来源已在未来 1–5 分钟内错峰重排。"
            )
        )
    except Exception:
        return fetch_settings_redirect(error="抓取策略保存失败，请输入 5 到 1440 的整数分钟数。")


@app.post("/settings/web-push-window")
def save_web_push_window(
    request: Request,
    window_hours: int = Form(...),
) -> RedirectResponse:
    try:
        hours = get_services(request).repository.save_web_push_window_hours(window_hours)
        return push_settings_redirect(notice=f"手机通知统计范围已设为最近 {hours} 小时。")
    except Exception:
        return push_settings_redirect(error="通知统计范围保存失败，请输入 1 到 24 的整数小时数。")


@app.get("/connections")
def connections_redirect(request: Request, notice: str = "", error: str = "") -> RedirectResponse:
    return settings_redirect(anchor="platforms", notice=notice, error=error)


@app.get("/settings/x-session")
def x_session_settings_redirect(request: Request, notice: str = "", error: str = "") -> RedirectResponse:
    return settings_redirect(anchor="x", notice=notice, error=error)


@app.post("/settings/x-session")
def save_x_session(request: Request, cookie_value: str = Form(...)) -> RedirectResponse:
    try:
        services = get_services(request)
        services.x_sessions.save_from_web(cookie_value)
        retried = services.sources.requeue_failed_platform_sources(SourceKind.X_RSSHUB)
        suffix = f"已安排 {retried} 个此前失败的 X 来源重新抓取。" if retried else ""
        return x_session_redirect(notice=f"RSSHub 已使用新 Cookie 完成 X 抓取验证。{suffix}")
    except XSessionError as exc:
        return x_session_redirect(error=str(exc))
    except Exception:
        return x_session_redirect(error="保存 X 登录 Cookie 时发生异常，请稍后重试。")


@app.post("/settings/x-session/test")
def test_x_session(request: Request) -> RedirectResponse:
    try:
        services = get_services(request)
        services.x_sessions.test_saved()
        retried = services.sources.requeue_failed_platform_sources(SourceKind.X_RSSHUB)
        suffix = f"已安排 {retried} 个此前失败的 X 来源重新抓取。" if retried else ""
        return x_session_redirect(notice=f"RSSHub 已使用当前 Cookie 完成 X 抓取验证。{suffix}")
    except XSessionError as exc:
        return x_session_redirect(error=str(exc))
    except Exception:
        return x_session_redirect(error="暂时无法验证 X 登录 Cookie，请稍后重试。")


@app.get("/settings/llm")
def llm_settings_redirect_legacy(request: Request, notice: str = "", error: str = "") -> RedirectResponse:
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
        (item for item in services.connections.source_connections() if item.key == connection), None
    )
    return render(
        request,
        "source_form.html",
        {
            "source": None,
            "choices": services.sources.form_choices(),
            "connections": services.connections.source_connections(),
            "required_connection": required_connection,
            "fetch_policy": services.repository.get_fetch_policy(),
            "error": error,
            "mode": "new",
        },
    )


def _batch_source_context(
    *,
    batch_result: Any | None = None,
    error: str = "",
) -> dict[str, Any]:
    return {
        "batch_result": batch_result,
        "error": error,
    }


@app.get("/sources/batch", response_class=HTMLResponse)
def batch_source_form(request: Request) -> HTMLResponse:
    return render(request, "source_batch.html", _batch_source_context())


@app.get("/sources/export.yml")
def export_sources(request: Request) -> Response:
    """下载当前全部来源；文件可以直接回传到批量添加入口。"""

    content = get_services(request).source_backups.export_text()
    return Response(
        content=content,
        media_type="application/x-yaml; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="newsrsshub-sources-export.yml"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/sources/backups/{filename}")
def download_source_backup(request: Request, filename: str) -> FileResponse:
    """只允许下载持久化目录中由服务创建的来源快照。"""

    path = get_services(request).source_backups.get_backup(filename)
    if not path:
        raise HTTPException(status_code=404, detail="未找到该来源备份。")
    return FileResponse(path, media_type="application/x-yaml", filename=path.name)


@app.get("/sources/batch/template.yml")
def download_batch_source_template(request: Request) -> Response:
    content = get_services(request).batch_sources.yaml_template()
    return Response(
        content=content,
        media_type="application/x-yaml; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="newsrsshub-sources-template.yml"'},
    )


@app.get("/sources/batch/recommended.yml")
def download_recommended_source_import(request: Request) -> FileResponse:
    """提供已经核验过格式的个人推荐来源包，供用户下载后再上传。"""

    path = Path(__file__).resolve().parents[1] / "sources" / "recommended-ai-sources-import.yml"
    return FileResponse(
        path,
        media_type="application/x-yaml",
        filename="recommended-ai-sources-import.yml",
    )


@app.post("/sources/batch", response_class=HTMLResponse)
async def batch_add_sources(
    request: Request,
    source_file: UploadFile = File(...),
) -> HTMLResponse:
    """校验上传文件后导入；文件永不落盘，启用来源只会进入随机排期。"""

    services = get_services(request)
    filename = (source_file.filename or "").strip()
    try:
        if not filename.lower().endswith((".yml", ".yaml")):
            raise ValueError("请上传 .yml 或 .yaml 文件。")
        payload = await source_file.read()
        if not payload:
            raise ValueError("上传的 YAML 文件为空。")
        if len(payload) > services.batch_sources.MAX_UPLOAD_BYTES:
            raise ValueError("YAML 文件不能超过 1 MB。")
        try:
            content = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("YAML 文件必须使用 UTF-8 编码。") from exc
        result = services.batch_sources.import_yaml(content)
        return render(
            request,
            "source_batch.html",
            _batch_source_context(batch_result=result),
        )
    except Exception as exc:
        return render(
            request,
            "source_batch.html",
            _batch_source_context(error=str(exc)),
            status_code=422,
        )
    finally:
        await source_file.close()


def _make_draft(
    *,
    name: str,
    kind: str,
    locator: str,
    description: str,
    is_official: bool,
    global_interval_minutes: int,
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
        description=description.strip()[:300],
        is_official=is_official,
        poll_interval_minutes=max(5, min(int(global_interval_minutes), 1440)),
        enabled=enabled,
    )


@app.post("/sources/new")
def create_source(
    request: Request,
    name: str = Form(...),
    kind: str = Form(...),
    locator: str = Form(...),
    description: str = Form(""),
    is_official: bool = Form(False),
    enabled: bool = Form(False),
) -> RedirectResponse:
    services = get_services(request)
    try:
        if kind == "auto":
            kind = services.sources.detect_kind(locator).value
        draft = _make_draft(
            name=name,
            kind=kind,
            locator=locator,
            description=description,
            is_official=is_official,
            global_interval_minutes=services.repository.get_fetch_policy().interval_minutes,
            enabled=enabled,
        )
        source, validation = services.sources.add_source(draft, validate=True)
        selected_kind = str(source["kind"])
        capture_state = "已启用抓取。" if source["enabled"] else "当前保持暂停。"
        if validation and validation.ok:
            return sources_redirect(
                notice=f"已添加并验证成功：{source['name']}。连接正常，{capture_state}",
                kind=selected_kind,
            )
        if validation:
            return sources_redirect(
                notice=(
                    f"已添加：{source['name']}。但连接测试未通过：{validation.message}；"
                    "来源已保留，可在列表中稍后重新测试。"
                ),
                notice_level="warning",
                kind=selected_kind,
            )
        return sources_redirect(notice=f"已添加：{source['name']}。{capture_state}", kind=selected_kind)
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
        {
            "source": source,
            "choices": services.sources.form_choices(),
            "fetch_policy": services.repository.get_fetch_policy(),
            "error": error,
            "mode": "edit",
        },
    )


@app.post("/sources/{source_id}/edit")
def edit_source(
    request: Request,
    source_id: int,
    name: str = Form(...),
    description: str = Form(""),
    is_official: bool = Form(False),
    enabled: bool = Form(False),
) -> RedirectResponse:
    services = get_services(request)
    source = services.repository.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="未找到来源")
    try:
        draft = _make_draft(
            name=name,
            kind=source["kind"],
            locator=source["locator"],
            description=description,
            is_official=is_official,
            global_interval_minutes=services.repository.get_fetch_policy().interval_minutes,
            enabled=enabled,
        )
        services.sources.update_source(source_id, draft)
        validation = services.sources.validate_source(source_id)
        if validation.ok:
            return sources_redirect(notice=f"来源配置已保存并验证：{validation.message}")
        return sources_redirect(error=f"来源配置已保存，但连接测试失败：{validation.message}")
    except Exception as exc:
        return RedirectResponse(
            f"/sources/{source_id}/edit?{urlencode({'error': str(exc)})}", status_code=303
        )


@app.post("/sources/test-current-page")
def queue_current_source_page_for_test(
    request: Request,
    source_kind: str = Form("all"),
    page: int = Form(1),
) -> RedirectResponse:
    """安排当前平台页的启用来源由后台 Worker 统一验证。"""

    selected_kind = _safe_source_kind(source_kind)
    selected_page = _source_page_value(page)
    if selected_kind == "all":
        return sources_redirect(error="请先选择一个具体平台，再测试当前页来源。")

    services = get_services(request)
    try:
        services.connections.ensure_source_ready(selected_kind)
    except ConnectionRequiredError as exc:
        return sources_redirect(
            error=f"无法测试 {exc.connection.platform} 来源：{exc.connection.message}",
            kind=selected_kind,
            page=selected_page,
        )

    source_page = services.repository.list_sources_page(
        kind=selected_kind,
        page=selected_page,
        page_size=SOURCE_PAGE_SIZE,
    )
    source_ids = [int(source["id"]) for source in source_page.sources if source["enabled"]]
    if not source_ids:
        return sources_redirect(
            error="当前页没有启用的来源，无法安排测试。",
            kind=selected_kind,
            page=source_page.page,
        )

    queued = services.sources.queue_sources_for_manual_test(source_ids)
    if not queued:
        return sources_redirect(
            error="当前页来源状态已变化，没有可测试的启用来源。",
            kind=selected_kind,
            page=source_page.page,
        )
    return sources_redirect(
        notice=f"已安排当前页 {queued} 个启用来源在下一轮后台抓取中测试；通常约 1 分钟后刷新页面查看结果。",
        kind=selected_kind,
        page=source_page.page,
    )


@app.post("/sources/{source_id}/test")
def test_source(
    request: Request,
    source_id: int,
    source_kind: str = Form("all"),
    page: int = Form(1),
) -> RedirectResponse:
    try:
        result = get_services(request).sources.validate_source(source_id)
        prefix = "连接正常：" if result.ok else "连接失败："
        return sources_redirect(notice=f"{prefix}{result.message}", kind=source_kind, page=page)
    except Exception as exc:
        return sources_redirect(error=str(exc), kind=source_kind, page=page)


@app.post("/sources/{source_id}/toggle")
def toggle_source(
    request: Request,
    source_id: int,
    source_kind: str = Form("all"),
    page: int = Form(1),
) -> RedirectResponse:
    repository = get_services(request).repository
    source = repository.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="未找到来源")
    enabling = not source["enabled"]
    repository.update_source(source_id, {"enabled": int(enabling)})
    if source["enabled"]:
        return sources_redirect(
            notice="来源已暂停；刷新首页或简报后，会立即隐藏它已有的内容。",
            kind=source_kind,
            page=page,
        )
    # 重新启用时不要沿用暂停前可能已过期的时间，避免多来源同时立刻抓取。
    repository.schedule_initial_fetch(source_id)
    return sources_redirect(
        notice="来源已启用，已安排在未来 1–5 分钟内错峰抓取。", kind=source_kind, page=page
    )


@app.post("/sources/{source_id}/archive")
def archive_source(
    request: Request,
    source_id: int,
    source_kind: str = Form("all"),
    page: int = Form(1),
) -> RedirectResponse:
    get_services(request).sources.archive_source(source_id)
    return sources_redirect(
        notice="来源已归档，可随时从数据库恢复。", kind=source_kind, page=page
    )
