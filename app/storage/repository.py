from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

from app.domain.curation import CurationGroup, EditorialTier
from app.domain.models import FetchPolicy, FeedItem, SourceDraft
from app.storage.database import Database


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_now() -> str:
    return utc_now().isoformat()


def _decode_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("event_ids_json", "config_json", "highlights_json", "media_json"):
        if key in data:
            data[key.removesuffix("_json")] = _decode_json(
                data[key], {} if key == "config_json" else []
            )
    for key in ("is_official", "enabled", "archived", "user_hidden", "user_read", "user_saved"):
        if key in data:
            data[key] = bool(data[key])
    return data


def _source_is_live_clause(source_alias: str = "s") -> str:
    return f"{source_alias}.enabled = 1 AND {source_alias}.archived = 0"


def _event_has_live_item_clause(event_alias: str = "e") -> str:
    return f"""
        EXISTS (
            SELECT 1
            FROM items visible_i
            JOIN sources visible_s ON visible_s.id = visible_i.source_id
            WHERE visible_i.event_id = {event_alias}.id
              AND {_source_is_live_clause('visible_s')}
        )
    """


def _user_hidden_clause(event_alias: str = "e") -> str:
    return f"""EXISTS (
        SELECT 1 FROM feedback hidden_feedback
        WHERE hidden_feedback.event_id = {event_alias}.id
          AND hidden_feedback.action = 'not_interested'
    )"""


def _event_saved_clause(event_alias: str = "e") -> str:
    return f"""EXISTS (
        SELECT 1 FROM feedback saved_feedback
        WHERE saved_feedback.event_id = {event_alias}.id
          AND saved_feedback.action = 'saved'
    )"""


CONTENT_RETENTION_DAYS = 30


@dataclass(frozen=True, slots=True)
class SourcePage:
    sources: list[dict[str, Any]]
    total: int
    page: int
    page_size: int

    @property
    def page_count(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)


class Repository:
    """Persistence boundary for the non-scoring personal news flow."""

    FETCH_INTERVAL_SETTING = "global_fetch_interval_minutes"
    MIN_FETCH_INTERVAL_MINUTES = 5
    MAX_FETCH_INTERVAL_MINUTES = 1440

    def __init__(self, database: Database) -> None:
        self.database = database

    # Sources -----------------------------------------------------------------
    def count_sources(self) -> int:
        with self.database.read() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM sources WHERE archived = 0").fetchone()[0])

    def list_sources(self, include_archived: bool = False) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE archived = 0"
        with self.database.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM sources {where} ORDER BY enabled DESC, name COLLATE NOCASE, id DESC"
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # 全局抓取策略 -------------------------------------------------------------
    @classmethod
    def _normalize_fetch_interval(cls, value: int | str) -> int:
        try:
            interval = int(value)
        except (TypeError, ValueError):
            interval = FetchPolicy().interval_minutes
        return max(cls.MIN_FETCH_INTERVAL_MINUTES, min(interval, cls.MAX_FETCH_INTERVAL_MINUTES))

    def get_fetch_policy(self) -> FetchPolicy:
        with self.database.read() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (self.FETCH_INTERVAL_SETTING,)
            ).fetchone()
        interval = self._normalize_fetch_interval(row["value"] if row else FetchPolicy().interval_minutes)
        return FetchPolicy(interval_minutes=interval)

    @staticmethod
    def _jitter_seconds(
        policy: FetchPolicy,
        jitter_provider: Callable[[int, int], int] | None = None,
    ) -> int:
        provider = jitter_provider or random.randint
        return int(provider(policy.jitter_min_seconds, policy.jitter_max_seconds))

    @classmethod
    def _next_fetch_time(
        cls,
        policy: FetchPolicy,
        *,
        now: datetime,
        jitter_provider: Callable[[int, int], int] | None = None,
    ) -> str:
        delay = timedelta(minutes=policy.interval_minutes) + timedelta(
            seconds=cls._jitter_seconds(policy, jitter_provider)
        )
        return (now + delay).isoformat()

    @classmethod
    def _initial_fetch_time(
        cls,
        policy: FetchPolicy,
        *,
        now: datetime,
        jitter_provider: Callable[[int, int], int] | None = None,
    ) -> str:
        """将首次任务分散到 1–5 分钟内，避免启用后形成请求尖峰。"""

        return (now + timedelta(seconds=cls._jitter_seconds(policy, jitter_provider))).isoformat()

    def schedule_unplanned_sources(
        self,
        policy: FetchPolicy | None = None,
        *,
        now: datetime | None = None,
        jitter_provider: Callable[[int, int], int] | None = None,
    ) -> int:
        """为尚未排期的启用来源写入持久化的首次抓取时间。"""

        policy = policy or self.get_fetch_policy()
        now = now or utc_now()
        with self.database.transaction() as conn:
            rows = conn.execute(
                """
                SELECT id FROM sources
                WHERE enabled = 1 AND archived = 0 AND next_fetch_at IS NULL
                ORDER BY id
                """
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE sources SET next_fetch_at = ?, updated_at = ? WHERE id = ?",
                    (
                        self._initial_fetch_time(policy, now=now, jitter_provider=jitter_provider),
                        now.isoformat(),
                        int(row["id"]),
                    ),
                )
        return len(rows)

    def save_fetch_policy(
        self,
        interval_minutes: int | str,
        *,
        now: datetime | None = None,
        jitter_provider: Callable[[int, int], int] | None = None,
    ) -> tuple[FetchPolicy, int]:
        """保存统一间隔，并在短随机窗口内重新排期所有启用来源。"""

        now = now or utc_now()
        policy = FetchPolicy(interval_minutes=self._normalize_fetch_interval(interval_minutes))
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (self.FETCH_INTERVAL_SETTING, str(policy.interval_minutes), now.isoformat()),
            )
            rows = conn.execute(
                "SELECT id FROM sources WHERE enabled = 1 AND archived = 0 ORDER BY id"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE sources SET next_fetch_at = ?, updated_at = ? WHERE id = ?",
                    (
                        self._initial_fetch_time(policy, now=now, jitter_provider=jitter_provider),
                        now.isoformat(),
                        int(row["id"]),
                    ),
                )
        return policy, len(rows)

    def schedule_next_fetch(
        self,
        source_id: int,
        policy: FetchPolicy | None = None,
        *,
        now: datetime | None = None,
        jitter_provider: Callable[[int, int], int] | None = None,
    ) -> str:
        policy = policy or self.get_fetch_policy()
        now = now or utc_now()
        next_fetch_at = self._next_fetch_time(policy, now=now, jitter_provider=jitter_provider)
        self.update_source(source_id, {"next_fetch_at": next_fetch_at})
        return next_fetch_at

    def schedule_initial_fetch(
        self,
        source_id: int,
        policy: FetchPolicy | None = None,
        *,
        now: datetime | None = None,
        jitter_provider: Callable[[int, int], int] | None = None,
    ) -> str:
        """为新建来源安排首次抓取，而不等待完整全局周期。"""

        policy = policy or self.get_fetch_policy()
        now = now or utc_now()
        next_fetch_at = self._initial_fetch_time(policy, now=now, jitter_provider=jitter_provider)
        self.update_source(source_id, {"next_fetch_at": next_fetch_at})
        return next_fetch_at

    def list_sources_page(
        self,
        *,
        kind: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> SourcePage:
        """List a stable, bounded source-management page by platform."""

        normalized_kind = str(kind or "").strip()
        page_size = max(5, min(int(page_size), 100))
        clauses = ["archived = 0"]
        values: list[Any] = []
        if normalized_kind:
            clauses.append("kind = ?")
            values.append(normalized_kind)
        where = " WHERE " + " AND ".join(clauses)

        with self.database.read() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM sources{where}", values).fetchone()[0])
            page_count = max(1, (total + page_size - 1) // page_size)
            current_page = min(max(1, int(page)), page_count)
            rows = conn.execute(
                f"SELECT * FROM sources{where} "
                "ORDER BY enabled DESC, name COLLATE NOCASE, id DESC LIMIT ? OFFSET ?",
                (*values, page_size, (current_page - 1) * page_size),
            ).fetchall()
        return SourcePage(
            sources=[_row_to_dict(row) for row in rows],
            total=total,
            page=current_page,
            page_size=page_size,
        )

    def source_kind_counts(self) -> dict[str, int]:
        with self.database.read() as conn:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS total FROM sources WHERE archived = 0 GROUP BY kind"
            ).fetchall()
        return {str(row["kind"]): int(row["total"]) for row in rows}

    def has_enabled_source_kind(self, kind: str) -> bool:
        with self.database.read() as conn:
            row = conn.execute(
                """
                SELECT EXISTS(
                    SELECT 1 FROM sources
                    WHERE kind = ? AND enabled = 1 AND archived = 0
                )
                """,
                (kind,),
            ).fetchone()
        return bool(row[0])

    def requeue_failed_sources_for_kind(self, kind: str) -> int:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE sources
                SET health_status = 'unknown', last_fetch_at = NULL, next_fetch_at = ?,
                    last_error = ?, updated_at = ?
                WHERE kind = ? AND enabled = 1 AND archived = 0 AND health_status = 'error'
                """,
                (iso_now(), "", iso_now(), kind),
            )
        return int(cursor.rowcount)

    def requeue_sources_for_fetch(self, source_ids: Sequence[int]) -> int:
        """将指定且仍启用的来源标记为下一轮后台抓取。"""

        ids = sorted({int(source_id) for source_id in source_ids if int(source_id) > 0})
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self.database.transaction() as conn:
            cursor = conn.execute(
                f"""
                UPDATE sources
                SET health_status = 'unknown', last_fetch_at = NULL, next_fetch_at = ?,
                    last_error = ?, updated_at = ?
                WHERE id IN ({placeholders}) AND enabled = 1 AND archived = 0
                """,
                (iso_now(), "", iso_now(), *ids),
            )
        return int(cursor.rowcount)

    def get_source(self, source_id: int) -> dict[str, Any] | None:
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def find_source(self, kind: str, locator: str) -> dict[str, Any] | None:
        with self.database.read() as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE kind = ? AND locator = ?", (kind, locator)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def create_source(self, draft: SourceDraft, feed_url: str) -> int:
        now = iso_now()
        with self.database.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM sources WHERE kind = ? AND locator = ?",
                (draft.kind.value, draft.locator),
            ).fetchone()
            if existing:
                raise ValueError("这个来源已经存在。")
            cursor = conn.execute(
                """
                INSERT INTO sources (
                    name, description, kind, locator, feed_url, is_official, enabled, archived,
                    poll_interval_minutes, health_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.name,
                    draft.description,
                    draft.kind.value,
                    draft.locator,
                    feed_url,
                    int(draft.is_official),
                    int(draft.enabled and not draft.archived),
                    int(draft.archived),
                    draft.poll_interval_minutes,
                    "archived" if draft.archived else "unknown",
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def update_source(self, source_id: int, values: dict[str, Any]) -> None:
        allowed = {
            "name",
            "description",
            "is_official",
            "enabled",
            "poll_interval_minutes",
            "next_fetch_at",
            "last_new_item_count",
            "feed_url",
            "health_status",
            "last_fetch_at",
            "last_success_at",
            "last_error",
            "archived",
        }
        selected = {key: value for key, value in values.items() if key in allowed}
        if not selected:
            return
        selected["updated_at"] = iso_now()
        assignments = ", ".join(f"{key} = ?" for key in selected)
        with self.database.transaction() as conn:
            conn.execute(
                f"UPDATE sources SET {assignments} WHERE id = ?", (*selected.values(), source_id)
            )

    def archive_source(self, source_id: int) -> None:
        self.update_source(source_id, {"archived": 1, "enabled": 0, "health_status": "archived"})

    def update_source_config(self, source_id: int, config: dict[str, Any]) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE sources SET config_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(config, ensure_ascii=False), iso_now(), source_id),
            )

    def due_sources(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or utc_now()
        with self.database.read() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sources
                WHERE enabled = 1 AND archived = 0
                  AND next_fetch_at IS NOT NULL AND next_fetch_at <= ?
                ORDER BY next_fetch_at ASC, id ASC
                """,
                (now.isoformat(),),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # Connector credentials ---------------------------------------------------
    def get_connector_credential(self, connector: str) -> dict[str, Any] | None:
        with self.database.read() as conn:
            row = conn.execute(
                "SELECT * FROM connector_credentials WHERE connector = ?", (connector,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def save_connector_credential(
        self,
        *,
        connector: str,
        ciphertext: str,
        fingerprint: str,
        status: str = "valid",
        last_validated_at: str | None = None,
    ) -> None:
        now = iso_now()
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO connector_credentials (
                    connector, ciphertext, fingerprint, status, last_validated_at,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, '', ?, ?)
                ON CONFLICT(connector) DO UPDATE SET
                    ciphertext = excluded.ciphertext,
                    fingerprint = excluded.fingerprint,
                    status = excluded.status,
                    last_validated_at = excluded.last_validated_at,
                    last_error = '',
                    updated_at = excluded.updated_at
                """,
                (connector, ciphertext, fingerprint, status, last_validated_at or now, now, now),
            )

    def update_connector_credential_health(
        self,
        connector: str,
        *,
        status: str,
        last_error: str = "",
        validated: bool = False,
    ) -> None:
        values: list[Any] = [status, last_error[:1000], iso_now()]
        assignments = "status = ?, last_error = ?, updated_at = ?"
        if validated:
            assignments += ", last_validated_at = ?"
            values.append(iso_now())
        values.append(connector)
        with self.database.transaction() as conn:
            conn.execute(
                f"UPDATE connector_credentials SET {assignments} WHERE connector = ?", values
            )

    def delete_connector_credential(self, connector: str) -> None:
        with self.database.transaction() as conn:
            conn.execute("DELETE FROM connector_credentials WHERE connector = ?", (connector,))

    # 原始条目 ----------------------------------------------------------------
    @staticmethod
    def _content_hash(item: FeedItem) -> str:
        payload = "\n".join((item.title.strip(), item.content.strip()))
        return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()

    def insert_item(self, source_id: int, item: FeedItem) -> tuple[int, bool]:
        published_at = item.published_at.isoformat() if item.published_at else None
        now = iso_now()
        content_hash = self._content_hash(item)
        media_json = json.dumps(item.media, ensure_ascii=False)
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO items (
                    source_id, guid, canonical_url, title, content, author, published_at,
                    fetched_at, content_hash, media_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    item.guid,
                    item.link,
                    item.title,
                    item.content,
                    item.author,
                    published_at,
                    now,
                    content_hash,
                    media_json,
                ),
            )
            if cursor.rowcount:
                return int(cursor.lastrowid), True
            row = conn.execute(
                "SELECT id, content_hash, event_id FROM items WHERE source_id = ? AND guid = ?",
                (source_id, item.guid),
            ).fetchone()
            if not row:
                raise RuntimeError("Duplicate item lookup did not return a row")
            item_id = int(row["id"])
            if str(row["content_hash"] or "") == content_hash:
                # 同 GUID 的重复抓取只回填媒体和抓取元数据，避免重复摘要与筛选。
                conn.execute(
                    """
                    UPDATE items
                    SET canonical_url = ?, author = ?, published_at = ?, fetched_at = ?, media_json = ?
                    WHERE id = ?
                    """,
                    (item.link, item.author, published_at, now, media_json, item_id),
                )
                return item_id, False

            # Feeds sometimes revise an item while keeping the same GUID.  The
            # revised body must follow the full summary -> Skill path again;
            # otherwise the reader would keep seeing a stale event forever.
            conn.execute(
                """
                UPDATE items
                SET canonical_url = ?, title = ?, content = ?, author = ?, published_at = ?,
                    fetched_at = ?, content_hash = ?, display_title = '', summary = '',
                    highlights_json = '[]', summary_status = 'pending', summary_error = '',
                    summary_version = 0, summarized_at = NULL, translated_content = '',
                    translation_status = 'pending', translation_error = '', translation_version = 0,
                    translated_at = NULL, media_json = ?
                WHERE id = ?
                """,
                (
                    item.link,
                    item.title,
                    item.content,
                    item.author,
                    published_at,
                    now,
                    content_hash,
                    media_json,
                    item_id,
                ),
            )
            if row["event_id"] is not None:
                conn.execute(
                    "UPDATE events SET curation_status = 'pending' WHERE id = ?",
                    (int(row["event_id"]),),
                )
            return item_id, True

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_dict(row) if row else None

    # Item summaries ----------------------------------------------------------
    def list_items_needing_summary(
        self, limit: int = 50, *, minimum_version: int = 1
    ) -> list[dict[str, Any]]:
        """Return live items whose Chinese reader-facing artifact is outdated."""

        limit = max(1, min(int(limit), 100))
        minimum_version = max(1, int(minimum_version))
        with self.database.read() as conn:
            rows = conn.execute(
                f"""
                SELECT i.*, s.name AS source_name, s.is_official
                FROM items i
                JOIN sources s ON s.id = i.source_id
                WHERE (i.summary_status IN ('pending', 'retry') OR i.summary_version < ?)
                  AND {_source_is_live_clause('s')}
                ORDER BY COALESCE(i.published_at, i.fetched_at) DESC, i.id DESC
                LIMIT ?
                """,
                (minimum_version, limit),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def save_item_summary(
        self,
        item_id: int,
        *,
        summary: str,
        display_title: str = "",
        highlights: Sequence[str] | None = None,
        status: str = "complete",
        error: str = "",
        version: int = 1,
    ) -> None:
        cleaned_highlights = [str(item).strip()[:240] for item in (highlights or []) if str(item).strip()]
        now = iso_now()
        with self.database.transaction() as conn:
            conn.execute(
                """
                UPDATE items
                SET display_title = ?, summary = ?, highlights_json = ?, summary_status = ?,
                    summary_error = ?, summary_version = ?, summarized_at = ?
                WHERE id = ?
                """,
                (
                    display_title[:300],
                    summary[:3000],
                    json.dumps(cleaned_highlights[:4], ensure_ascii=False),
                    status,
                    error[:1000],
                    version,
                    now,
                    item_id,
                ),
            )
            # A refreshed reader artifact can change the Chinese event title
            # and summary.  Recurate an existing event rather than leaving the
            # previous English display representation in place.
            conn.execute(
                """
                UPDATE events
                SET curation_status = 'pending'
                WHERE id = (SELECT event_id FROM items WHERE id = ?) AND curation_status = 'complete'
                """,
                (item_id,),
            )

    def mark_item_summary_retry(self, item_id: int, error: str) -> None:
        self.save_item_summary(item_id, summary="", status="retry", error=error, version=0)

    # Chinese translations ---------------------------------------------------
    def save_item_translation(
        self,
        item_id: int,
        *,
        translated_content: str,
        status: str = "complete",
        error: str = "",
        version: int = 1,
    ) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                """
                UPDATE items
                SET translated_content = ?, translation_status = ?, translation_error = ?,
                    translation_version = ?, translated_at = ?
                WHERE id = ?
                """,
                (translated_content[:60_000], status, error[:1000], version, iso_now(), item_id),
            )

    def mark_item_translation_retry(self, item_id: int, error: str) -> None:
        self.save_item_translation(
            item_id,
            translated_content="",
            status="retry",
            error=error,
            version=0,
        )

    def list_primary_items_needing_translation(
        self, limit: int = 12, *, minimum_version: int = 1
    ) -> list[dict[str, Any]]:
        """Return visible primary bodies that should be translated proactively."""

        limit = max(1, min(int(limit), 30))
        minimum_version = max(1, int(minimum_version))
        with self.database.read() as conn:
            rows = conn.execute(
                f"""
                SELECT i.*
                FROM events e
                JOIN items i ON i.id = e.primary_item_id
                JOIN sources s ON s.id = i.source_id
                WHERE e.curation_status = 'complete'
                  AND e.editorial_tier IN ('must_read', 'important')
                  AND i.content <> ''
                  AND (i.translation_status IN ('pending', 'retry') OR i.translation_version < ?)
                  AND {_source_is_live_clause('s')}
                ORDER BY e.curation_order ASC, e.last_seen_at DESC, e.id DESC
                LIMIT ?
                """,
                (minimum_version, limit),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_items_for_curation(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return only the minimal fields allowed into the project Skill."""

        limit = max(1, min(int(limit), 50))
        with self.database.read() as conn:
            rows = conn.execute(
                f"""
                SELECT i.id, i.title, i.summary, i.published_at, i.fetched_at, i.event_id
                FROM items i
                JOIN sources s ON s.id = i.source_id
                LEFT JOIN events e ON e.id = i.event_id
                WHERE i.summary_status = 'complete'
                  AND {_source_is_live_clause('s')}
                  AND (i.event_id IS NULL OR e.curation_status IN ('pending', 'retry', 'failed'))
                ORDER BY COALESCE(i.published_at, i.fetched_at) DESC, i.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_recent_explicit_feedback(
        self, *, days: int = 5, limit: int = 60
    ) -> list[dict[str, Any]]:
        """返回可供筛选 Skill 使用的近期显式用户行为。"""

        days = max(1, min(int(days), 31))
        limit = max(1, min(int(limit), 100))
        cutoff = (utc_now() - timedelta(days=days)).isoformat()
        with self.database.read() as conn:
            rows = conn.execute(
                """
                WITH recent_actions AS (
                    SELECT event_id, action, MAX(created_at) AS acted_at
                    FROM feedback
                    WHERE event_id IS NOT NULL
                      AND action IN ('read', 'not_interested')
                      AND created_at >= ?
                    GROUP BY event_id, action
                )
                SELECT action_row.action, action_row.acted_at, e.title, e.summary
                FROM recent_actions action_row
                JOIN events e ON e.id = action_row.event_id
                WHERE action_row.action = 'not_interested'
                   OR (
                        action_row.action = 'read'
                    -- 新内容到达后，旧摘要的阅读行为不再代表当前事件版本。
                    AND acted_at >= e.last_seen_at
                    -- 同一事件有近期明确负反馈时，避免向 Skill 传递矛盾信号。
                    AND NOT EXISTS (
                        SELECT 1
                        FROM recent_actions negative_action
                        WHERE negative_action.event_id = action_row.event_id
                          AND negative_action.action = 'not_interested'
                    )
                   )
                ORDER BY
                    CASE action_row.action WHEN 'not_interested' THEN 0 ELSE 1 END,
                    action_row.acted_at DESC,
                    e.id DESC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # Curation ----------------------------------------------------------------
    def _event_for_group(self, conn: Any, item_ids: Sequence[int]) -> tuple[int | None, list[int]]:
        placeholders = ", ".join("?" for _ in item_ids)
        rows = conn.execute(
            f"SELECT DISTINCT event_id FROM items WHERE id IN ({placeholders}) AND event_id IS NOT NULL",
            tuple(item_ids),
        ).fetchall()
        event_ids = sorted(int(row[0]) for row in rows if row[0] is not None)
        return (event_ids[0] if event_ids else None, event_ids)

    def _merge_event_rows(self, conn: Any, target_event_id: int, redundant_event_ids: Sequence[int]) -> None:
        for event_id in redundant_event_ids:
            if event_id == target_event_id:
                continue
            conn.execute("UPDATE items SET event_id = ? WHERE event_id = ?", (target_event_id, event_id))
            feedback_rows = conn.execute(
                "SELECT action, created_at FROM feedback WHERE event_id = ?", (event_id,)
            ).fetchall()
            for feedback in feedback_rows:
                conn.execute(
                    """
                    INSERT INTO feedback (event_id, action, created_at) VALUES (?, ?, ?)
                    ON CONFLICT(event_id, action) DO UPDATE SET
                        created_at = MAX(feedback.created_at, excluded.created_at)
                    """,
                    (target_event_id, str(feedback["action"]), str(feedback["created_at"])),
                )
            conn.execute("DELETE FROM feedback WHERE event_id = ?", (event_id,))
            conn.execute("DELETE FROM events WHERE id = ?", (event_id,))

    def apply_curation_groups(self, groups: Sequence[CurationGroup]) -> list[int]:
        """Apply a validated Skill result in one database transaction.

        Existing events encountered in a group are merged before all group items
        are attached. This lets a later batch add evidence to an existing event
        without title matching or score heuristics.
        """

        now = iso_now()
        event_ids: list[int] = []
        with self.database.transaction() as conn:
            for group in groups:
                item_ids = list(group.item_ids)
                primary = conn.execute(
                    "SELECT * FROM items WHERE id = ?", (group.primary_item_id,)
                ).fetchone()
                if not primary:
                    raise ValueError("筛选结果引用了不存在的主条目。")
                display_title = str(primary["display_title"] or primary["title"])
                target_event_id, existing_event_ids = self._event_for_group(conn, item_ids)
                if target_event_id is None:
                    cursor = conn.execute(
                        """
                        INSERT INTO events (
                            title, summary, editorial_tier, tier_reason, curation_order,
                            curation_status, curated_at, primary_item_id, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, 'complete', ?, ?, ?, ?)
                        """,
                        (
                            display_title,
                            str(primary["summary"]),
                            group.tier.value,
                            group.reason,
                            group.order,
                            now,
                            group.primary_item_id,
                            now,
                            now,
                        ),
                    )
                    target_event_id = int(cursor.lastrowid)
                else:
                    self._merge_event_rows(conn, target_event_id, existing_event_ids)

                placeholders = ", ".join("?" for _ in item_ids)
                conn.execute(
                    f"UPDATE items SET event_id = ? WHERE id IN ({placeholders})",
                    (target_event_id, *item_ids),
                )

                aggregate = conn.execute(
                    """
                    SELECT
                        MIN(COALESCE(published_at, fetched_at)) AS first_seen_at,
                        MAX(COALESCE(published_at, fetched_at)) AS last_seen_at
                    FROM items WHERE event_id = ?
                    """,
                    (target_event_id,),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE events
                    SET title = ?, summary = ?, editorial_tier = ?, tier_reason = ?,
                        curation_order = ?, curation_status = 'complete', curated_at = ?,
                        primary_item_id = ?, first_seen_at = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        display_title,
                        str(primary["summary"]),
                        group.tier.value,
                        group.reason,
                        group.order,
                        now,
                        group.primary_item_id,
                        str(aggregate["first_seen_at"] or now),
                        str(aggregate["last_seen_at"] or now),
                        target_event_id,
                    ),
                )
                event_ids.append(target_event_id)
        return event_ids

    def mark_curation_retry(self, item_ids: Sequence[int], error: str) -> None:
        if not item_ids:
            return
        placeholders = ", ".join("?" for _ in item_ids)
        with self.database.transaction() as conn:
            conn.execute(
                f"""
                UPDATE events SET curation_status = 'retry'
                WHERE id IN (SELECT DISTINCT event_id FROM items WHERE id IN ({placeholders}) AND event_id IS NOT NULL)
                """,
                tuple(item_ids),
            )

    def primary_items_for_events(self, event_ids: Sequence[int]) -> list[dict[str, Any]]:
        if not event_ids:
            return []
        placeholders = ", ".join("?" for _ in event_ids)
        with self.database.read() as conn:
            rows = conn.execute(
                f"""
                SELECT i.id, i.title, i.summary, i.published_at, i.fetched_at, i.event_id
                FROM events e
                JOIN items i ON i.id = e.primary_item_id
                WHERE e.id IN ({placeholders}) AND i.summary_status = 'complete'
                ORDER BY e.id
                """,
                tuple(event_ids),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def recent_primary_items(self, *, hours: int = 72, limit: int = 40) -> list[dict[str, Any]]:
        """Compact existing event context used only for cross-batch merging.

        These are still ordinary item summaries, so the Skill receives the same
        minimal contract as the first pass rather than raw event/source data.
        """

        cutoff = (utc_now() - timedelta(hours=max(1, hours))).isoformat()
        with self.database.read() as conn:
            rows = conn.execute(
                """
                SELECT i.id, i.title, i.summary, i.published_at, i.fetched_at, i.event_id
                FROM events e
                JOIN items i ON i.id = e.primary_item_id
                WHERE e.curation_status = 'complete'
                  AND e.last_seen_at >= ?
                  AND i.summary_status = 'complete'
                ORDER BY e.last_seen_at DESC, e.id DESC
                LIMIT ?
                """,
                (cutoff, max(1, min(int(limit), 50))),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # Reader-facing events ----------------------------------------------------
    def _event_list_query(
        self,
        *,
        tier: EditorialTier | str,
        period: str,
        include_user_hidden: bool = False,
    ) -> tuple[str, list[Any]]:
        try:
            requested_tier = EditorialTier(tier)
        except ValueError as exc:
            raise ValueError("无效的内容层级") from exc
        cutoff_map = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        clauses = ["e.curation_status = 'complete'", _event_has_live_item_clause("e")]
        values: list[Any] = []
        if period in cutoff_map:
            clauses.append("e.last_seen_at >= ?")
            values.append((utc_now() - cutoff_map[period]).isoformat())

        hidden = _user_hidden_clause("e")
        if requested_tier == EditorialTier.HIDDEN:
            clauses.append(f"(e.editorial_tier = 'hidden' OR {hidden})")
        else:
            clauses.append("e.editorial_tier = ?")
            values.append(requested_tier.value)
            if not include_user_hidden:
                clauses.append(f"NOT {hidden}")
        return " AND ".join(clauses), values

    def list_events(
        self,
        *,
        tier: EditorialTier | str = EditorialTier.MUST_READ,
        period: str = "24h",
        limit: int = 50,
        offset: int = 0,
        include_user_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        where, values = self._event_list_query(
            tier=tier, period=period, include_user_hidden=include_user_hidden
        )
        query = f"""
            SELECT e.*,
                EXISTS(SELECT 1 FROM feedback f WHERE f.event_id = e.id AND f.action = 'not_interested') AS user_hidden,
                EXISTS(
                    SELECT 1 FROM feedback f
                    WHERE f.event_id = e.id
                      AND f.action = 'read'
                      AND f.created_at >= e.last_seen_at
                ) AS user_read,
                {_event_saved_clause('e')} AS user_saved,
                (
                    SELECT s.name
                    FROM items i
                    JOIN sources s ON s.id = i.source_id
                    WHERE i.event_id = e.id AND {_source_is_live_clause('s')}
                    ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
                    LIMIT 1
                ) AS primary_source_name,
                (
                    SELECT i.canonical_url
                    FROM items i
                    JOIN sources s ON s.id = i.source_id
                    WHERE i.event_id = e.id AND {_source_is_live_clause('s')}
                    ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
                    LIMIT 1
                ) AS primary_url,
                (
                    SELECT MAX(i.published_at)
                    FROM items i
                    JOIN sources s ON s.id = i.source_id
                    WHERE i.event_id = e.id AND {_source_is_live_clause('s')}
                ) AS latest_published_at,
                (
                    SELECT MAX(i.fetched_at)
                    FROM items i
                    JOIN sources s ON s.id = i.source_id
                    WHERE i.event_id = e.id AND {_source_is_live_clause('s')}
                ) AS latest_fetched_at,
                (
                    SELECT i.highlights_json
                    FROM items i
                    WHERE i.id = e.primary_item_id
                ) AS highlights_json,
                (
                    SELECT COUNT(DISTINCT i.source_id)
                    FROM items i
                    JOIN sources s ON s.id = i.source_id
                    WHERE i.event_id = e.id AND {_source_is_live_clause('s')}
                ) AS visible_source_count
            FROM events e
            WHERE {where}
            ORDER BY e.curation_order ASC, e.last_seen_at DESC, e.id DESC
            LIMIT ? OFFSET ?
        """
        with self.database.read() as conn:
            rows = conn.execute(query, (*values, limit, max(0, int(offset)))).fetchall()
        return [_row_to_dict(row) for row in rows]

    def count_events(
        self,
        *,
        tier: EditorialTier | str = EditorialTier.MUST_READ,
        period: str = "24h",
        include_user_hidden: bool = False,
    ) -> int:
        where, values = self._event_list_query(
            tier=tier, period=period, include_user_hidden=include_user_hidden
        )
        with self.database.read() as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM events e WHERE {where}", values).fetchone()
        return int(row[0])

    def tier_counts(self, period: str = "24h") -> dict[str, int]:
        result: dict[str, int] = {}
        for tier in (EditorialTier.MUST_READ, EditorialTier.IMPORTANT, EditorialTier.BRIEF, EditorialTier.HIDDEN):
            result[tier.value] = self.count_events(tier=tier, period=period)
        return result

    def list_saved_events(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """返回收藏事件，即使其来源后来被停用或归档也仍可阅读。"""

        limit = max(1, min(int(limit), 50))
        query = f"""
            SELECT e.*,
                EXISTS(SELECT 1 FROM feedback f WHERE f.event_id = e.id AND f.action = 'not_interested') AS user_hidden,
                EXISTS(
                    SELECT 1 FROM feedback f
                    WHERE f.event_id = e.id
                      AND f.action = 'read'
                      AND f.created_at >= e.last_seen_at
                ) AS user_read,
                1 AS user_saved,
                (
                    SELECT s.name
                    FROM items i
                    JOIN sources s ON s.id = i.source_id
                    WHERE i.event_id = e.id
                    ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
                    LIMIT 1
                ) AS primary_source_name,
                (
                    SELECT i.highlights_json
                    FROM items i
                    WHERE i.id = e.primary_item_id
                ) AS highlights_json,
                (
                    SELECT MAX(f.created_at)
                    FROM feedback f
                    WHERE f.event_id = e.id AND f.action = 'saved'
                ) AS saved_at
            FROM events e
            WHERE {_event_saved_clause('e')}
            ORDER BY saved_at DESC, e.last_seen_at DESC, e.id DESC
            LIMIT ? OFFSET ?
        """
        with self.database.read() as conn:
            rows = conn.execute(query, (limit, max(0, int(offset)))).fetchall()
        return [_row_to_dict(row) for row in rows]

    def count_saved_events(self) -> int:
        with self.database.read() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM events e WHERE {_event_saved_clause('e')}"
            ).fetchone()
        return int(row[0])

    def is_event_saved(self, event_id: int) -> bool:
        with self.database.read() as conn:
            row = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM feedback WHERE event_id = ? AND action = 'saved')",
                (event_id,),
            ).fetchone()
        return bool(row[0])

    def get_event(
        self, event_id: int, *, include_inactive_sources: bool = False
    ) -> dict[str, Any] | None:
        with self.database.read() as conn:
            event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not event:
                return None
            source_clause = "1 = 1" if include_inactive_sources else _source_is_live_clause("s")
            items = conn.execute(
                f"""
                SELECT i.*, s.name AS source_name, s.kind AS source_kind, s.is_official
                FROM items i
                JOIN sources s ON s.id = i.source_id
                WHERE i.event_id = ? AND {source_clause}
                ORDER BY CASE WHEN i.id = ? THEN 0 ELSE 1 END,
                         COALESCE(i.published_at, i.fetched_at) DESC
                """,
                (event_id, event["primary_item_id"]),
            ).fetchall()
            user_hidden = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM feedback WHERE event_id = ? AND action = 'not_interested')",
                (event_id,),
            ).fetchone()[0]
            user_saved = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM feedback WHERE event_id = ? AND action = 'saved')",
                (event_id,),
            ).fetchone()[0]
            visible_source_count = conn.execute(
                f"""
                SELECT COUNT(DISTINCT i.source_id)
                FROM items i JOIN sources s ON s.id = i.source_id
                WHERE i.event_id = ? AND {source_clause}
                """,
                (event_id,),
            ).fetchone()[0]
        if not items:
            return None
        data = _row_to_dict(event)
        data["items"] = [_row_to_dict(item) for item in items]
        data["highlights"] = data["items"][0].get("highlights", [])
        data["user_hidden"] = bool(user_hidden)
        data["user_saved"] = bool(user_saved)
        data["visible_source_count"] = int(visible_source_count)
        return data

    def mark_event_not_interested(self, event_id: int) -> None:
        now = iso_now()
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO feedback (event_id, action, created_at) VALUES (?, 'not_interested', ?)
                ON CONFLICT(event_id, action) DO UPDATE SET created_at = excluded.created_at
                """,
                (event_id, now),
            )

    def mark_event_read(self, event_id: int) -> None:
        """记录用户已主动阅读过该事件，并保持每个事件只有一条已读记录。"""

        # 保留微秒，确保刚完成筛选的事件可在同一秒内被立即标记为已读。
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO feedback (event_id, action, created_at) VALUES (?, 'read', ?)
                ON CONFLICT(event_id, action) DO UPDATE SET created_at = excluded.created_at
                """,
                (event_id, now),
            )

    def save_event(self, event_id: int) -> None:
        """以事件为单位收藏，复用复合主键保证状态只有一条。"""

        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO feedback (event_id, action, created_at) VALUES (?, 'saved', ?)
                ON CONFLICT(event_id, action) DO UPDATE SET created_at = excluded.created_at
                """,
                (event_id, iso_now()),
            )

    def unsave_event(self, event_id: int) -> None:
        with self.database.transaction() as conn:
            conn.execute("DELETE FROM feedback WHERE event_id = ? AND action = 'saved'", (event_id,))

    def restore_event(self, event_id: int) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                "DELETE FROM feedback WHERE event_id = ? AND action = 'not_interested'", (event_id,)
            )

    # Briefs and dashboard ----------------------------------------------------
    def list_briefs(self) -> list[dict[str, Any]]:
        with self.database.read() as conn:
            rows = conn.execute("SELECT * FROM briefs ORDER BY brief_date DESC LIMIT 90").fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_brief(self, brief_date: str) -> dict[str, Any] | None:
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM briefs WHERE brief_date = ?", (brief_date,)).fetchone()
        return _row_to_dict(row) if row else None

    def upsert_brief(self, brief_date: date, title: str, intro: str, event_ids: Iterable[int]) -> None:
        now = iso_now()
        payload = json.dumps(list(event_ids), ensure_ascii=False)
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO briefs (brief_date, title, intro, event_ids_json, generated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(brief_date) DO UPDATE SET
                    title = excluded.title,
                    intro = excluded.intro,
                    event_ids_json = excluded.event_ids_json,
                    generated_at = excluded.generated_at
                """,
                (brief_date.isoformat(), title, intro, payload, now),
            )

    def get_events_by_ids(self, event_ids: Iterable[int]) -> list[dict[str, Any]]:
        ids = list(event_ids)
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self.database.read() as conn:
            rows = conn.execute(
                f"""
                SELECT e.* FROM events e
                WHERE e.id IN ({placeholders})
                  AND e.curation_status = 'complete'
                  AND {_event_has_live_item_clause('e')}
                  AND NOT {_user_hidden_clause('e')}
                """,
                ids,
            ).fetchall()
        by_id = {int(row["id"]): _row_to_dict(row) for row in rows}
        return [by_id[event_id] for event_id in ids if event_id in by_id]

    def purge_expired_content(self, now: datetime | None = None) -> dict[str, int]:
        """清理过期内容，同时保留收藏事件和仍被日报引用的事件。"""

        current_time = now or utc_now()
        event_cutoff = (current_time - timedelta(days=CONTENT_RETENTION_DAYS)).isoformat()
        # 日报保留包含今天在内的最近 30 个自然日，再读取剩余日报的有效引用。
        brief_cutoff = (current_time.date() - timedelta(days=CONTENT_RETENTION_DAYS)).isoformat()

        with self.database.transaction() as conn:
            deleted_briefs = conn.execute(
                "DELETE FROM briefs WHERE brief_date <= ?", (brief_cutoff,)
            ).rowcount

            protected_event_ids: set[int] = set()
            has_unknown_brief_references = False
            brief_rows = conn.execute("SELECT event_ids_json FROM briefs").fetchall()
            for brief_row in brief_rows:
                event_ids = _decode_json(brief_row["event_ids_json"], None)
                if not isinstance(event_ids, list):
                    has_unknown_brief_references = True
                    continue
                for referenced_event_id in event_ids:
                    try:
                        protected_event_ids.add(int(referenced_event_id))
                    except (TypeError, ValueError):
                        continue

            expired_rows = conn.execute(
                f"""
                SELECT e.id FROM events e
                WHERE e.last_seen_at < ?
                  AND NOT {_event_saved_clause('e')}
                """,
                (event_cutoff,),
            ).fetchall()
            # 保留期内的日报若损坏，无法可靠识别其引用；此时宁可多保留事件。
            expired_event_ids: list[int] = []
            if not has_unknown_brief_references:
                expired_event_ids = [
                    int(row["id"])
                    for row in expired_rows
                    if int(row["id"]) not in protected_event_ids
                ]

            deleted_event_items = 0
            if expired_event_ids:
                placeholders = ", ".join("?" for _ in expired_event_ids)
                deleted_event_items = conn.execute(
                    f"DELETE FROM items WHERE event_id IN ({placeholders})", expired_event_ids
                ).rowcount
                # feedback 对事件设置了级联删除；这里先删除事件即可保持状态一致。
                conn.execute(f"DELETE FROM events WHERE id IN ({placeholders})", expired_event_ids)

            # 尚未归入事件的旧帖子没有收藏入口，可在同一保留期内直接清理。
            deleted_orphan_items = conn.execute(
                """
                DELETE FROM items
                WHERE event_id IS NULL
                  AND COALESCE(published_at, fetched_at) < ?
                """,
                (event_cutoff,),
            ).rowcount

        return {
            "briefs": max(0, int(deleted_briefs)),
            "events": len(expired_event_ids),
            "items": max(0, int(deleted_event_items)) + max(0, int(deleted_orphan_items)),
        }

    def dashboard_stats(self) -> dict[str, int]:
        now = utc_now()
        with self.database.read() as conn:
            source_count = conn.execute(
                "SELECT COUNT(*) FROM sources WHERE enabled = 1 AND archived = 0"
            ).fetchone()[0]
            healthy_count = conn.execute(
                """
                SELECT COUNT(*) FROM sources
                WHERE health_status = 'healthy' AND enabled = 1 AND archived = 0
                """
            ).fetchone()[0]
            event_count = conn.execute(
                f"""
                SELECT COUNT(*) FROM events e
                WHERE e.curation_status = 'complete'
                  AND e.last_seen_at >= ?
                  AND {_event_has_live_item_clause('e')}
                  AND NOT {_user_hidden_clause('e')}
                """,
                ((now - timedelta(hours=24)).isoformat(),),
            ).fetchone()[0]
            pending_summary = conn.execute(
                "SELECT COUNT(*) FROM items WHERE summary_status IN ('pending', 'retry')"
            ).fetchone()[0]
            pending_curation = conn.execute(
                """
                SELECT COUNT(*) FROM items i
                LEFT JOIN events e ON e.id = i.event_id
                WHERE i.summary_status = 'complete'
                  AND (i.event_id IS NULL OR e.curation_status IN ('pending', 'retry', 'failed'))
                """
            ).fetchone()[0]
            pending_translation = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM events e
                JOIN items i ON i.id = e.primary_item_id
                JOIN sources s ON s.id = i.source_id
                WHERE e.curation_status = 'complete'
                  AND e.editorial_tier IN ('must_read', 'important')
                  AND i.content <> ''
                  AND i.translation_status IN ('pending', 'retry')
                  AND {_source_is_live_clause('s')}
                """
            ).fetchone()[0]
        return {
            "source_count": int(source_count),
            "healthy_count": int(healthy_count),
            "event_count": int(event_count),
            "pending_summary": int(pending_summary),
            "pending_curation": int(pending_curation),
            "pending_translation": int(pending_translation),
        }
