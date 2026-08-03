from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from app.domain.curation import CurationGroup, EditorialTier
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
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("raw_json", "event_ids_json", "config_json"):
        if key in data:
            data[key.removesuffix("_json")] = _decode_json(
                data[key], {} if key == "config_json" else []
            )
    for key in ("is_official", "enabled", "archived"):
        if key in data:
            data[key] = bool(data[key])
    return data


def _source_is_live_clause(source_alias: str = "s") -> str:
    return f"{source_alias}.enabled = 1 AND {source_alias}.archived = 0"


def _event_has_live_item_clause(event_alias: str = "e") -> str:
    return f"""
        EXISTS (
            SELECT 1
            FROM event_items visible_ei
            JOIN items visible_i ON visible_i.id = visible_ei.item_id
            JOIN sources visible_s ON visible_s.id = visible_i.source_id
            WHERE visible_ei.event_id = {event_alias}.id
              AND {_source_is_live_clause('visible_s')}
        )
    """


def _user_hidden_clause(event_alias: str = "e") -> str:
    return f"""EXISTS (
        SELECT 1 FROM feedback hidden_feedback
        WHERE hidden_feedback.event_id = {event_alias}.id
          AND hidden_feedback.action = 'not_interested'
    )"""


class Repository:
    """Persistence boundary for the non-scoring personal news flow."""

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
                    name, kind, locator, feed_url, is_official, enabled,
                    poll_interval_minutes, fallback_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.name,
                    draft.kind.value,
                    draft.locator,
                    feed_url,
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
            "name",
            "is_official",
            "enabled",
            "poll_interval_minutes",
            "fallback_url",
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
        due: list[dict[str, Any]] = []
        for source in self.list_sources():
            if not source["enabled"]:
                continue
            last_fetch = source.get("last_fetch_at")
            if not last_fetch:
                due.append(source)
                continue
            try:
                parsed = datetime.fromisoformat(str(last_fetch))
                parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                due.append(source)
                continue
            if now - parsed >= timedelta(minutes=int(source["poll_interval_minutes"])):
                due.append(source)
        return due

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

    # Fetch runs and raw items ------------------------------------------------
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
                """
                UPDATE fetch_runs
                SET finished_at = ?, status = ?, new_item_count = ?, message = ?
                WHERE id = ?
                """,
                (iso_now(), status, new_item_count, message[:1000], run_id),
            )

    @staticmethod
    def _content_hash(item: FeedItem) -> str:
        payload = "\n".join((item.title.strip(), item.content.strip()))
        return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()

    def insert_item(self, source_id: int, item: FeedItem) -> tuple[int, bool]:
        published_at = item.published_at.isoformat() if item.published_at else None
        now = iso_now()
        content_hash = self._content_hash(item)
        raw_json = json.dumps(item.raw, ensure_ascii=False)
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO items (
                    source_id, guid, canonical_url, title, content, author, published_at,
                    fetched_at, content_hash, raw_json
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
                    raw_json,
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
                return item_id, False

            # Feeds sometimes revise an item while keeping the same GUID.  The
            # revised body must follow the full summary -> Skill path again;
            # otherwise the reader would keep seeing a stale event forever.
            conn.execute(
                """
                UPDATE items
                SET canonical_url = ?, title = ?, content = ?, author = ?, published_at = ?,
                    fetched_at = ?, content_hash = ?, summary = '', summary_status = 'pending',
                    summary_error = '', summary_version = 0, summarized_at = NULL, raw_json = ?
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
                    raw_json,
                    item_id,
                ),
            )
            if row["event_id"] is not None:
                conn.execute(
                    "UPDATE events SET curation_status = 'pending', updated_at = ? WHERE id = ?",
                    (now, int(row["event_id"])),
                )
            return item_id, True

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_dict(row) if row else None

    # Item summaries ----------------------------------------------------------
    def list_items_needing_summary(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.database.read() as conn:
            rows = conn.execute(
                f"""
                SELECT i.*, s.name AS source_name, s.is_official
                FROM items i
                JOIN sources s ON s.id = i.source_id
                WHERE i.summary_status IN ('pending', 'retry')
                  AND {_source_is_live_clause('s')}
                ORDER BY COALESCE(i.published_at, i.fetched_at) DESC, i.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def save_item_summary(
        self,
        item_id: int,
        *,
        summary: str,
        status: str = "complete",
        error: str = "",
        version: int = 1,
    ) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                """
                UPDATE items
                SET summary = ?, summary_status = ?, summary_error = ?, summary_version = ?,
                    summarized_at = ?
                WHERE id = ?
                """,
                (summary[:3000], status, error[:1000], version, iso_now(), item_id),
            )

    def mark_item_summary_retry(self, item_id: int, error: str) -> None:
        self.save_item_summary(item_id, summary="", status="retry", error=error, version=0)

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

    # Curation ----------------------------------------------------------------
    def start_curation_run(self, input_count: int) -> int:
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO curation_runs (status, input_count, started_at)
                VALUES ('running', ?, ?)
                """,
                (input_count, iso_now()),
            )
            return int(cursor.lastrowid)

    def finish_curation_run(
        self, run_id: int, *, status: str, event_count: int = 0, message: str = ""
    ) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                """
                UPDATE curation_runs
                SET status = ?, event_count = ?, message = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, event_count, message[:1000], iso_now(), run_id),
            )

    @staticmethod
    def _fingerprint_for_item(item_id: int) -> str:
        return f"curated-item-{item_id}"

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
            conn.execute(
                "INSERT OR IGNORE INTO event_items (event_id, item_id) "
                "SELECT ?, item_id FROM event_items WHERE event_id = ?",
                (target_event_id, event_id),
            )
            conn.execute("UPDATE feedback SET event_id = ? WHERE event_id = ?", (target_event_id, event_id))
            conn.execute("DELETE FROM event_items WHERE event_id = ?", (event_id,))
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
                target_event_id, existing_event_ids = self._event_for_group(conn, item_ids)
                if target_event_id is None:
                    cursor = conn.execute(
                        """
                        INSERT INTO events (
                            fingerprint, title, summary, editorial_tier, tier_reason,
                            curation_order, curation_status, curated_at, curation_version,
                            primary_item_id, source_count, first_seen_at, last_seen_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'complete', ?, 1, ?, 1, ?, ?, ?, ?)
                        """,
                        (
                            self._fingerprint_for_item(group.primary_item_id),
                            str(primary["title"]),
                            str(primary["summary"]),
                            group.tier.value,
                            group.reason,
                            group.order,
                            now,
                            group.primary_item_id,
                            now,
                            now,
                            now,
                            now,
                        ),
                    )
                    target_event_id = int(cursor.lastrowid)
                else:
                    self._merge_event_rows(conn, target_event_id, existing_event_ids)

                placeholders = ", ".join("?" for _ in item_ids)
                conn.execute(f"DELETE FROM event_items WHERE item_id IN ({placeholders})", tuple(item_ids))
                conn.execute(
                    f"UPDATE items SET event_id = ? WHERE id IN ({placeholders})",
                    (target_event_id, *item_ids),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO event_items (event_id, item_id) VALUES (?, ?)",
                    [(target_event_id, item_id) for item_id in item_ids],
                )

                aggregate = conn.execute(
                    """
                    SELECT
                        COUNT(DISTINCT source_id) AS source_count,
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
                        curation_version = curation_version + 1, primary_item_id = ?,
                        source_count = ?, first_seen_at = ?, last_seen_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(primary["title"]),
                        str(primary["summary"]),
                        group.tier.value,
                        group.reason,
                        group.order,
                        now,
                        group.primary_item_id,
                        int(aggregate["source_count"] or 1),
                        str(aggregate["first_seen_at"] or now),
                        str(aggregate["last_seen_at"] or now),
                        now,
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
                UPDATE events SET curation_status = 'retry', updated_at = ?
                WHERE id IN (SELECT DISTINCT event_id FROM items WHERE id IN ({placeholders}) AND event_id IS NOT NULL)
                """,
                (iso_now(), *item_ids),
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
                (
                    SELECT s.name
                    FROM event_items ei
                    JOIN items i ON i.id = ei.item_id
                    JOIN sources s ON s.id = i.source_id
                    WHERE ei.event_id = e.id AND {_source_is_live_clause('s')}
                    ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
                    LIMIT 1
                ) AS primary_source_name,
                (
                    SELECT i.canonical_url
                    FROM event_items ei
                    JOIN items i ON i.id = ei.item_id
                    JOIN sources s ON s.id = i.source_id
                    WHERE ei.event_id = e.id AND {_source_is_live_clause('s')}
                    ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
                    LIMIT 1
                ) AS primary_url,
                (
                    SELECT COUNT(DISTINCT i.source_id)
                    FROM event_items ei
                    JOIN items i ON i.id = ei.item_id
                    JOIN sources s ON s.id = i.source_id
                    WHERE ei.event_id = e.id AND {_source_is_live_clause('s')}
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

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self.database.read() as conn:
            event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not event:
                return None
            items = conn.execute(
                f"""
                SELECT i.*, s.name AS source_name, s.kind AS source_kind, s.is_official
                FROM event_items ei
                JOIN items i ON i.id = ei.item_id
                JOIN sources s ON s.id = i.source_id
                WHERE ei.event_id = ? AND {_source_is_live_clause('s')}
                ORDER BY CASE WHEN i.id = ? THEN 0 ELSE 1 END,
                         COALESCE(i.published_at, i.fetched_at) DESC
                """,
                (event_id, event["primary_item_id"]),
            ).fetchall()
            user_hidden = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM feedback WHERE event_id = ? AND action = 'not_interested')",
                (event_id,),
            ).fetchone()[0]
        if not items:
            return None
        data = _row_to_dict(event)
        data["items"] = [_row_to_dict(item) for item in items]
        data["user_hidden"] = bool(user_hidden)
        return data

    def mark_event_not_interested(self, event_id: int) -> None:
        now = iso_now()
        with self.database.transaction() as conn:
            conn.execute(
                "DELETE FROM feedback WHERE event_id = ? AND action = 'not_interested'", (event_id,)
            )
            conn.execute(
                "INSERT INTO feedback (event_id, action, created_at) VALUES (?, 'not_interested', ?)",
                (event_id, now),
            )

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
        return {
            "source_count": int(source_count),
            "healthy_count": int(healthy_count),
            "event_count": int(event_count),
            "pending_summary": int(pending_summary),
            "pending_curation": int(pending_curation),
        }
