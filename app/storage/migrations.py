"""Versioned SQLite schema migrations.

The project used to rely on ``CREATE TABLE IF NOT EXISTS`` only.  That leaves
existing installations on an old shape forever, which is especially dangerous
when a UI field has been removed.  This module owns the upgrade path and uses
``PRAGMA user_version`` to make every step safe to run repeatedly.
"""

from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 4


SOURCES_TABLE = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    locator TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    is_official INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 60,
    fallback_url TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    health_status TEXT NOT NULL DEFAULT 'unknown',
    last_fetch_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(kind, locator)
);
"""


EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    editorial_tier TEXT NOT NULL DEFAULT 'pending',
    tier_reason TEXT NOT NULL DEFAULT '',
    curation_order INTEGER NOT NULL DEFAULT 9999,
    curation_status TEXT NOT NULL DEFAULT 'pending',
    curated_at TEXT,
    curation_version INTEGER NOT NULL DEFAULT 1,
    primary_item_id INTEGER,
    source_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


ITEMS_TABLE = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
    guid TEXT NOT NULL,
    canonical_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    summary_status TEXT NOT NULL DEFAULT 'pending',
    summary_error TEXT NOT NULL DEFAULT '',
    summary_version INTEGER NOT NULL DEFAULT 0,
    summarized_at TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_id, guid)
);
"""


LATEST_SCHEMA = SOURCES_TABLE + EVENTS_TABLE + ITEMS_TABLE + """

CREATE TABLE IF NOT EXISTS fetch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    new_item_count INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS event_items (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    PRIMARY KEY(event_id, item_id)
);

CREATE TABLE IF NOT EXISTS curation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    input_count INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    intro TEXT NOT NULL,
    event_ids_json TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_credentials (
    connector TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_validated_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


INDEXES = """
DROP INDEX IF EXISTS idx_events_rank;
CREATE INDEX IF NOT EXISTS idx_sources_due ON sources(enabled, archived, last_fetch_at);
CREATE INDEX IF NOT EXISTS idx_items_source_guid ON items(source_id, guid);
CREATE INDEX IF NOT EXISTS idx_items_event ON items(event_id);
CREATE INDEX IF NOT EXISTS idx_items_summary ON items(summary_status, summarized_at);
CREATE INDEX IF NOT EXISTS idx_events_tier ON events(editorial_tier, curation_order, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_curation ON events(curation_status, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_fetch_runs_source ON fetch_runs(source_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_curation_runs_status ON curation_runs(status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_connector_credentials_status ON connector_credentials(status);
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
    )


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _column_expression(columns: set[str], name: str, fallback: str) -> str:
    return name if name in columns else fallback


def _rebuild_sources_without_category_or_priority(conn: sqlite3.Connection) -> None:
    """Drop obsolete source metadata while retaining every operational field.

    SQLite cannot drop columns on the versions commonly shipped in lightweight
    Docker images.  A copy-and-swap retains source IDs, so existing items,
    fetch runs and credentials remain valid.
    """

    columns = _columns(conn, "sources")
    if not columns or not ({"category", "priority"} & columns):
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(SOURCES_TABLE.replace("sources", "sources_new", 1))
        fields = (
            "id, name, kind, locator, feed_url, is_official, enabled, archived, "
            "poll_interval_minutes, fallback_url, config_json, health_status, "
            "last_fetch_at, last_success_at, last_error, created_at, updated_at"
        )
        values = ", ".join(
            (
                _column_expression(columns, "id", "NULL"),
                _column_expression(columns, "name", "''"),
                _column_expression(columns, "kind", "'rss'"),
                _column_expression(columns, "locator", "''"),
                _column_expression(columns, "feed_url", "''"),
                _column_expression(columns, "is_official", "0"),
                _column_expression(columns, "enabled", "1"),
                _column_expression(columns, "archived", "0"),
                _column_expression(columns, "poll_interval_minutes", "60"),
                _column_expression(columns, "fallback_url", "''"),
                _column_expression(columns, "config_json", "'{}'"),
                _column_expression(columns, "health_status", "'unknown'"),
                _column_expression(columns, "last_fetch_at", "NULL"),
                _column_expression(columns, "last_success_at", "NULL"),
                _column_expression(columns, "last_error", "''"),
                _column_expression(columns, "created_at", "CURRENT_TIMESTAMP"),
                _column_expression(columns, "updated_at", "CURRENT_TIMESTAMP"),
            )
        )
        conn.execute(f"INSERT INTO sources_new ({fields}) SELECT {values} FROM sources")
        conn.execute("DROP TABLE sources")
        conn.execute("ALTER TABLE sources_new RENAME TO sources")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


_LEGACY_EVENT_COLUMNS = frozenset(
    {
        "why_matters",
        "tags_json",
        "importance_score",
        "confidence",
        "analysis_status",
        "analysis_version",
    }
)
_LEGACY_ITEM_COLUMNS = frozenset({"relevance_score", "tags_json", "blacklisted"})


def _copy_and_replace_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    create_statement: str,
    fields: tuple[str, ...],
    columns: set[str],
    fallbacks: dict[str, str],
) -> None:
    """Copy a known SQLite table into its current target shape.

    All names passed here are application constants.  The helper deliberately
    keeps the copy expressions explicit, so a legacy installation can be
    upgraded even if it skipped an intermediate schema version.
    """

    replacement = f"{table}_new"
    conn.execute(create_statement.replace(table, replacement, 1))
    values = ", ".join(
        _column_expression(columns, field, fallbacks[field]) for field in fields
    )
    conn.execute(
        f"INSERT INTO {replacement} ({', '.join(fields)}) "
        f"SELECT {values} FROM {table}"
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {replacement} RENAME TO {table}")


def _rebuild_content_tables_without_legacy_scoring(conn: sqlite3.Connection) -> None:
    """Physically remove retired score/tag/analysis fields after data migration.

    Item and event identities, raw content, summaries, event membership and
    user feedback survive the copy.  Only derived fields from the retired
    keyword-scoring and generic-analysis pipeline are discarded.
    """

    event_columns = _columns(conn, "events")
    item_columns = _columns(conn, "items")
    rebuild_events = bool(event_columns & _LEGACY_EVENT_COLUMNS)
    rebuild_items = bool(item_columns & _LEGACY_ITEM_COLUMNS)
    drop_analyses = _table_exists(conn, "analyses")
    if not (rebuild_events or rebuild_items or drop_analyses):
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        if rebuild_events:
            _copy_and_replace_table(
                conn,
                table="events",
                create_statement=EVENTS_TABLE,
                fields=(
                    "id",
                    "fingerprint",
                    "title",
                    "summary",
                    "editorial_tier",
                    "tier_reason",
                    "curation_order",
                    "curation_status",
                    "curated_at",
                    "curation_version",
                    "primary_item_id",
                    "source_count",
                    "first_seen_at",
                    "last_seen_at",
                    "created_at",
                    "updated_at",
                ),
                columns=event_columns,
                fallbacks={
                    "id": "NULL",
                    "fingerprint": "printf('legacy-event-%s', id)",
                    "title": "''",
                    "summary": "''",
                    "editorial_tier": "'pending'",
                    "tier_reason": "''",
                    "curation_order": "9999",
                    "curation_status": "'pending'",
                    "curated_at": "NULL",
                    "curation_version": "1",
                    "primary_item_id": "NULL",
                    "source_count": "1",
                    "first_seen_at": "CURRENT_TIMESTAMP",
                    "last_seen_at": "CURRENT_TIMESTAMP",
                    "created_at": "CURRENT_TIMESTAMP",
                    "updated_at": "CURRENT_TIMESTAMP",
                },
            )
        if rebuild_items:
            _copy_and_replace_table(
                conn,
                table="items",
                create_statement=ITEMS_TABLE,
                fields=(
                    "id",
                    "source_id",
                    "event_id",
                    "guid",
                    "canonical_url",
                    "title",
                    "content",
                    "author",
                    "published_at",
                    "fetched_at",
                    "content_hash",
                    "summary",
                    "summary_status",
                    "summary_error",
                    "summary_version",
                    "summarized_at",
                    "raw_json",
                ),
                columns=item_columns,
                fallbacks={
                    "id": "NULL",
                    "source_id": "0",
                    "event_id": "NULL",
                    "guid": "printf('legacy-item-%s', id)",
                    "canonical_url": "''",
                    "title": "''",
                    "content": "''",
                    "author": "''",
                    "published_at": "NULL",
                    "fetched_at": "CURRENT_TIMESTAMP",
                    "content_hash": "''",
                    "summary": "''",
                    "summary_status": "'pending'",
                    "summary_error": "''",
                    "summary_version": "0",
                    "summarized_at": "NULL",
                    "raw_json": "'{}'",
                },
            )
        if drop_analyses:
            conn.execute("DROP TABLE analyses")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _upgrade_existing_schema(conn: sqlite3.Connection) -> None:
    """Bring the pre-Skill schema up to the target non-scoring shape."""

    _rebuild_sources_without_category_or_priority(conn)

    # Add the target fields before rebuilding legacy tables so all old rows can
    # be copied into a complete, retryable summary/curation state.
    for name, definition in (
        ("content_hash", "TEXT NOT NULL DEFAULT ''"),
        ("summary", "TEXT NOT NULL DEFAULT ''"),
        ("summary_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("summary_error", "TEXT NOT NULL DEFAULT ''"),
        ("summary_version", "INTEGER NOT NULL DEFAULT 0"),
        ("summarized_at", "TEXT"),
    ):
        _ensure_column(conn, "items", name, definition)
    for name, definition in (
        ("editorial_tier", "TEXT NOT NULL DEFAULT 'pending'"),
        ("tier_reason", "TEXT NOT NULL DEFAULT ''"),
        ("curation_order", "INTEGER NOT NULL DEFAULT 9999"),
        ("curation_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("curated_at", "TEXT"),
        ("curation_version", "INTEGER NOT NULL DEFAULT 1"),
    ):
        _ensure_column(conn, "events", name, definition)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS curation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            input_count INTEGER NOT NULL DEFAULT 0,
            event_count INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    # Legacy events were built with keyword and score logic. They must be sent
    # through the Skill before reappearing, never mapped from an old score.
    conn.execute(
        "UPDATE events SET editorial_tier = 'pending', curation_status = 'pending' "
        "WHERE curation_status IS NULL OR curation_status = ''"
    )
    _rebuild_content_tables_without_legacy_scoring(conn)


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Create a fresh schema or upgrade an existing database in place."""

    has_sources = _table_exists(conn, "sources")
    conn.executescript(LATEST_SCHEMA)
    if has_sources:
        _upgrade_existing_schema(conn)
    conn.executescript(INDEXES)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
