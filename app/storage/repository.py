from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from app.domain.models import FeedItem, SourceDraft
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
    except json.JSONDecodeError:
        return fallback


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("tags_json", "raw_json", "payload_json", "event_ids_json", "config_json"):
        if key in data:
            data[key.removesuffix("_json")] = _decode_json(data[key], [] if key != "config_json" else {})
    for key in ("is_official", "enabled", "archived", "blacklisted"):
        if key in data:
            data[key] = bool(data[key])
    return data


def _visible_event_clause(event_alias: str = "e") -> str:
    """Only show events that still have a live, non-blacklisted source item."""

    return f"""
        EXISTS (
            SELECT 1
            FROM event_items visible_ei
            JOIN items visible_i ON visible_i.id = visible_ei.item_id
            JOIN sources visible_s ON visible_s.id = visible_i.source_id
            WHERE visible_ei.event_id = {event_alias}.id
              AND visible_s.enabled = 1
              AND visible_s.archived = 0
              AND visible_i.blacklisted = 0
        )
        AND NOT EXISTS (
            SELECT 1
            FROM feedback visible_feedback
            WHERE visible_feedback.event_id = {event_alias}.id
              AND visible_feedback.action = 'not_interested'
        )
    """


def _event_row_to_dict(row: Any) -> dict[str, Any]:
    """Expose the analyzed Chinese headline without replacing the raw source title."""

    data = _row_to_dict(row)
    payload = _decode_json(data.pop("display_payload_json", None), {})
    headline = str(payload.get("headline") or "").strip() if isinstance(payload, dict) else ""
    data["display_title"] = headline or str(data.get("title") or "")
    return data


class Repository:
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
                f"SELECT * FROM sources {where} ORDER BY enabled DESC, priority DESC, name COLLATE NOCASE"
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def has_enabled_source_kind(self, kind: str) -> bool:
        """Return whether a live source currently depends on a platform kind."""

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
        """Clear obsolete platform failures so the worker checks them immediately.

        A successful shared-login check proves only the platform session.  Each
        source still needs its own fetch before it can be marked healthy, so it
        moves to ``unknown`` rather than incorrectly becoming healthy here.
        """

        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE sources
                SET health_status = 'unknown', last_fetch_at = NULL, last_error = ?, updated_at = ?
                WHERE kind = ? AND enabled = 1 AND archived = 0 AND health_status = 'error'
                """,
                ("", iso_now(), kind),
            )
        return int(cursor.rowcount)

    def get_source(self, source_id: int) -> dict[str, Any] | None:
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def find_source(self, kind: str, locator: str) -> dict[str, Any] | None:
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM sources WHERE kind = ? AND locator = ?", (kind, locator)).fetchone()
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
                    name, kind, locator, feed_url, category, priority, is_official,
                    enabled, poll_interval_minutes, fallback_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.name,
                    draft.kind.value,
                    draft.locator,
                    feed_url,
                    draft.category,
                    draft.priority,
                    int(draft.is_official),
                    int(draft.enabled),
                    draft.poll_interval_minutes,
                    draft.fallback_url,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def update_source(self, source_id: int, values: dict[str, Any]) -> None:
        allowed = {
            "name", "category", "priority", "is_official", "enabled",
            "poll_interval_minutes", "fallback_url", "feed_url", "health_status",
            "last_fetch_at", "last_success_at", "last_error", "archived",
        }
        selected = {key: value for key, value in values.items() if key in allowed}
        if not selected:
            return
        selected["updated_at"] = iso_now()
        assignments = ", ".join(f"{key} = ?" for key in selected)
        with self.database.transaction() as conn:
            conn.execute(
                f"UPDATE sources SET {assignments} WHERE id = ?",
                (*selected.values(), source_id),
            )

    def archive_source(self, source_id: int) -> None:
        self.update_source(source_id, {"archived": 1, "enabled": 0, "health_status": "archived"})

    def update_source_config(self, source_id: int, config: dict[str, Any]) -> None:
        """Persist non-secret connector state, such as an X numeric user id."""

        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE sources SET config_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(config, ensure_ascii=False), iso_now(), source_id),
            )

    def due_sources(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or utc_now()
        due: list[dict[str, Any]] = []
        for source in self.list_sources():
            if not source["enabled"]:
                continue
            if not source["last_fetch_at"]:
                due.append(source)
                continue
            try:
                last_fetch = datetime.fromisoformat(source["last_fetch_at"])
                if last_fetch.tzinfo is None:
                    last_fetch = last_fetch.replace(tzinfo=timezone.utc)
            except ValueError:
                due.append(source)
                continue
            if now - last_fetch >= timedelta(minutes=int(source["poll_interval_minutes"])):
                due.append(source)
        return due

    # Connector credentials ---------------------------------------------------
    # The encrypted payload is deliberately kept separate from a source's
    # configuration. Source rows are routinely displayed in the UI; credentials
    # must never be part of those records.
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

    # Fetch runs and items -----------------------------------------------------
    def start_fetch_run(self, source_id: int) -> int:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO fetch_runs (source_id, started_at, status) VALUES (?, ?, 'running')",
                (source_id, iso_now()),
            )
            return int(cursor.lastrowid)

    def finish_fetch_run(self, run_id: int, status: str, new_item_count: int, message: str = "") -> None:
        with self.database.transaction() as conn:
            conn.execute(
                "UPDATE fetch_runs SET finished_at = ?, status = ?, new_item_count = ?, message = ? WHERE id = ?",
                (iso_now(), status, new_item_count, message[:1000], run_id),
            )

    def insert_item(self, source_id: int, item: FeedItem, score: float, tags: list[str], blacklisted: bool) -> tuple[int, bool]:
        published_at = item.published_at.isoformat() if item.published_at else None
        now = iso_now()
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO items (
                    source_id, guid, canonical_url, title, content, author, published_at,
                    fetched_at, relevance_score, tags_json, blacklisted, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id, item.guid, item.link, item.title, item.content, item.author,
                    published_at, now, score, json.dumps(tags, ensure_ascii=False),
                    int(blacklisted), json.dumps(item.raw, ensure_ascii=False),
                ),
            )
            if cursor.rowcount:
                return int(cursor.lastrowid), True
            row = conn.execute(
                "SELECT id FROM items WHERE source_id = ? AND guid = ?", (source_id, item.guid)
            ).fetchone()
            return int(row[0]), False

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def mark_event_not_interested(self, event_id: int) -> None:
        """Hide one event from reader-facing lists without deleting its history."""

        now = iso_now()
        with self.database.transaction() as conn:
            conn.execute(
                "DELETE FROM feedback WHERE event_id = ? AND action = 'not_interested'",
                (event_id,),
            )
            conn.execute(
                "INSERT INTO feedback (event_id, action, created_at) VALUES (?, 'not_interested', ?)",
                (event_id, now),
            )

    # Events ------------------------------------------------------------------
    def find_event_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM events WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return _row_to_dict(row) if row else None

    def recent_event_candidates(self, hours: int = 72, limit: int = 120) -> list[dict[str, Any]]:
        cutoff = (utc_now() - timedelta(hours=hours)).isoformat()
        with self.database.read() as conn:
            rows = conn.execute(
                """
                SELECT id, title, fingerprint, last_seen_at
                FROM events
                WHERE last_seen_at >= ?
                ORDER BY last_seen_at DESC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def create_event(self, fingerprint: str, item: dict[str, Any]) -> int:
        now = iso_now()
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO events (
                    fingerprint, title, summary, why_matters, tags_json, importance_score,
                    primary_item_id, first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint,
                    item["title"],
                    item.get("summary", ""),
                    item.get("why_matters", ""),
                    json.dumps(item.get("tags", []), ensure_ascii=False),
                    item.get("relevance_score", 0),
                    item["id"],
                    now,
                    now,
                    now,
                    now,
                ),
            )
            event_id = int(cursor.lastrowid)
            conn.execute("UPDATE items SET event_id = ? WHERE id = ?", (event_id, item["id"]))
            conn.execute("INSERT INTO event_items (event_id, item_id) VALUES (?, ?)", (event_id, item["id"]))
            return event_id

    def attach_item_to_event(self, event_id: int, item_id: int) -> None:
        with self.database.transaction() as conn:
            conn.execute("UPDATE items SET event_id = ? WHERE id = ?", (event_id, item_id))
            conn.execute("INSERT OR IGNORE INTO event_items (event_id, item_id) VALUES (?, ?)", (event_id, item_id))
            conn.execute(
                "UPDATE events SET analysis_status = 'pending', updated_at = ? WHERE id = ?",
                (iso_now(), event_id),
            )

    def refresh_event(self, event_id: int) -> None:
        with self.database.transaction() as conn:
            aggregate = conn.execute(
                """
                SELECT
                    MAX(i.relevance_score) AS max_score,
                    COUNT(DISTINCT i.source_id) AS source_count,
                    MAX(i.fetched_at) AS last_seen_at,
                    MAX(i.id) AS newest_item_id
                FROM event_items ei
                JOIN items i ON i.id = ei.item_id
                WHERE ei.event_id = ?
                """,
                (event_id,),
            ).fetchone()
            primary = conn.execute(
                """
                SELECT i.* FROM event_items ei
                JOIN items i ON i.id = ei.item_id
                WHERE ei.event_id = ?
                ORDER BY i.relevance_score DESC, COALESCE(i.published_at, i.fetched_at) DESC
                LIMIT 1
                """,
                (event_id,),
            ).fetchone()
            if not primary or not aggregate:
                return
            tags: list[str] = []
            for row in conn.execute(
                "SELECT tags_json FROM items WHERE event_id = ?", (event_id,)
            ).fetchall():
                for tag in _decode_json(row[0], []):
                    if tag not in tags:
                        tags.append(tag)
            source_count = int(aggregate["source_count"] or 1)
            importance = min(100.0, float(aggregate["max_score"] or 0) + max(0, source_count - 1) * 8.0)
            conn.execute(
                """
                UPDATE events
                SET title = ?, tags_json = ?, importance_score = ?, primary_item_id = ?,
                    source_count = ?, last_seen_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    primary["title"], json.dumps(tags, ensure_ascii=False), importance,
                    primary["id"], source_count, aggregate["last_seen_at"], iso_now(), event_id,
                ),
            )

    def list_events(
        self,
        *,
        period: str = "24h",
        topic: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        cutoff_map = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        clauses: list[str] = []
        values: list[Any] = []
        clauses.append(_visible_event_clause("e"))
        if period in cutoff_map:
            clauses.append("e.last_seen_at >= ?")
            values.append((utc_now() - cutoff_map[period]).isoformat())
        if topic:
            clauses.append("e.tags_json LIKE ?")
            values.append(f"%{topic}%")
        where = f"WHERE {' AND '.join(clauses)}"
        query = f"""
            SELECT e.*, s.name AS primary_source_name, s.kind AS primary_source_kind,
                   i.published_at AS primary_published_at, i.canonical_url AS primary_url,
                   a.payload_json AS display_payload_json,
                   (
                       SELECT COUNT(DISTINCT visible_i.source_id)
                       FROM event_items visible_ei
                       JOIN items visible_i ON visible_i.id = visible_ei.item_id
                       JOIN sources visible_s ON visible_s.id = visible_i.source_id
                       WHERE visible_ei.event_id = e.id
                         AND visible_s.enabled = 1
                         AND visible_s.archived = 0
                         AND visible_i.blacklisted = 0
                   ) AS visible_source_count
            FROM events e
            LEFT JOIN items i ON i.id = (
                SELECT visible_i.id
                FROM event_items visible_ei
                JOIN items visible_i ON visible_i.id = visible_ei.item_id
                JOIN sources visible_s ON visible_s.id = visible_i.source_id
                WHERE visible_ei.event_id = e.id
                  AND visible_s.enabled = 1
                  AND visible_s.archived = 0
                  AND visible_i.blacklisted = 0
                ORDER BY visible_i.relevance_score DESC, COALESCE(visible_i.published_at, visible_i.fetched_at) DESC
                LIMIT 1
            )
            LEFT JOIN sources s ON s.id = i.source_id
            LEFT JOIN analyses a ON a.id = (
                SELECT latest_a.id
                FROM analyses latest_a
                WHERE latest_a.event_id = e.id
                ORDER BY latest_a.version DESC, latest_a.id DESC
                LIMIT 1
            )
            {where}
            ORDER BY e.importance_score DESC, e.last_seen_at DESC
            LIMIT ? OFFSET ?
        """
        with self.database.read() as conn:
            rows = conn.execute(query, (*values, limit, offset)).fetchall()
        return [_event_row_to_dict(row) for row in rows]

    def count_events(self, period: str = "24h", topic: str = "") -> int:
        cutoff_map = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        clauses: list[str] = [_visible_event_clause("e")]
        values: list[Any] = []
        if period in cutoff_map:
            clauses.append("e.last_seen_at >= ?")
            values.append((utc_now() - cutoff_map[period]).isoformat())
        if topic:
            clauses.append("e.tags_json LIKE ?")
            values.append(f"%{topic}%")
        where = f"WHERE {' AND '.join(clauses)}"
        with self.database.read() as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM events e {where}", values).fetchone()
        return int(row[0])

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self.database.read() as conn:
            event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not event:
                return None
            items = conn.execute(
                """
                SELECT i.*, s.name AS source_name, s.kind AS source_kind, s.is_official
                FROM event_items ei
                JOIN items i ON i.id = ei.item_id
                JOIN sources s ON s.id = i.source_id
                WHERE ei.event_id = ?
                ORDER BY i.relevance_score DESC, COALESCE(i.published_at, i.fetched_at) DESC
                """,
                (event_id,),
            ).fetchall()
            analysis = conn.execute(
                "SELECT * FROM analyses WHERE event_id = ? ORDER BY version DESC, id DESC LIMIT 1",
                (event_id,),
            ).fetchone()
        data = _row_to_dict(event)
        data["items"] = [_row_to_dict(item) for item in items]
        data["analysis"] = _row_to_dict(analysis) if analysis else None
        return data

    def list_pending_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.read() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM events
                WHERE analysis_status IN ('pending', 'retry')
                  AND {_visible_event_clause('events')}
                ORDER BY importance_score DESC, last_seen_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def requeue_unlocalized_headlines(self, limit: int = 20) -> int:
        """Give existing English-only headlines one upgraded model pass.

        Version two is a one-time localization retry.  It avoids turning a
        temporary model failure into a repeated request on every worker cycle.
        """

        with self.database.read() as conn:
            rows = conn.execute(
                f"""
                SELECT e.id, a.payload_json
                FROM events e
                JOIN analyses a ON a.id = (
                    SELECT latest_a.id
                    FROM analyses latest_a
                    WHERE latest_a.event_id = e.id
                    ORDER BY latest_a.version DESC, latest_a.id DESC
                    LIMIT 1
                )
                WHERE e.analysis_version < 2
                  AND e.analysis_status IN ('complete', 'fallback')
                  AND {_visible_event_clause('e')}
                ORDER BY e.importance_score DESC, e.last_seen_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 50)),),
            ).fetchall()

        event_ids: list[int] = []
        for row in rows:
            payload = _decode_json(row["payload_json"], {})
            headline = str(payload.get("headline") or "") if isinstance(payload, dict) else ""
            if not re.search(r"[\u4e00-\u9fff]", headline):
                event_ids.append(int(row["id"]))
        if not event_ids:
            return 0

        placeholders = ", ".join("?" for _ in event_ids)
        with self.database.transaction() as conn:
            conn.execute(
                f"UPDATE events SET analysis_status = 'retry', updated_at = ? WHERE id IN ({placeholders})",
                (iso_now(), *event_ids),
            )
        return len(event_ids)

    def save_analysis(
        self,
        event_id: int,
        *,
        provider: str,
        model: str,
        payload: dict[str, Any],
        status: str = "complete",
    ) -> None:
        now = iso_now()
        with self.database.transaction() as conn:
            version_row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM analyses WHERE event_id = ?", (event_id,)
            ).fetchone()
            version = int(version_row[0])
            conn.execute(
                "INSERT INTO analyses (event_id, provider, model, version, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, provider, model, version, json.dumps(payload, ensure_ascii=False), now),
            )
            conn.execute(
                """
                UPDATE events
                SET summary = ?, why_matters = ?, confidence = ?, analysis_status = ?,
                    analysis_version = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(payload.get("summary", ""))[:1500],
                    str(payload.get("why_it_matters", ""))[:1000],
                    str(payload.get("confidence", "待分析"))[:40],
                    status,
                    version,
                    now,
                    event_id,
                ),
            )

    # Briefs and dashboard -----------------------------------------------------
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
                SELECT e.*, a.payload_json AS display_payload_json
                FROM events e
                LEFT JOIN analyses a ON a.id = (
                    SELECT latest_a.id
                    FROM analyses latest_a
                    WHERE latest_a.event_id = e.id
                    ORDER BY latest_a.version DESC, latest_a.id DESC
                    LIMIT 1
                )
                WHERE e.id IN ({placeholders})
                  AND {_visible_event_clause('e')}
                """,
                ids,
            ).fetchall()
        by_id = {int(row["id"]): _event_row_to_dict(row) for row in rows}
        return [by_id[event_id] for event_id in ids if event_id in by_id]

    def dashboard_stats(self) -> dict[str, int]:
        now = utc_now()
        with self.database.read() as conn:
            source_count = conn.execute(
                "SELECT COUNT(*) FROM sources WHERE enabled = 1 AND archived = 0"
            ).fetchone()[0]
            healthy_count = conn.execute(
                "SELECT COUNT(*) FROM sources WHERE health_status = 'healthy' AND enabled = 1 AND archived = 0"
            ).fetchone()[0]
            event_count = conn.execute(
                f"""
                SELECT COUNT(*) FROM events e
                WHERE e.last_seen_at >= ? AND {_visible_event_clause('e')}
                """,
                ((now - timedelta(hours=24)).isoformat(),),
            ).fetchone()[0]
        return {"source_count": int(source_count), "healthy_count": int(healthy_count), "event_count": int(event_count)}
