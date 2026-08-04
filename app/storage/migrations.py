"""NewsRSSHub 固定使用的 SQLite v7 结构。

首次启动会创建空的 v7 数据库；已有数据库只做结构校验，绝不在服务启动时
自动删表、改表或执行历史数据迁移。
"""

from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 7

TARGET_TABLES = (
    "sources",
    "events",
    "items",
    "briefs",
    "feedback",
    "connector_credentials",
)


SOURCES_TABLE = """
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    locator TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    is_official INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 60,
    config_json TEXT NOT NULL DEFAULT '{}',
    health_status TEXT NOT NULL DEFAULT 'unknown',
    last_fetch_at TEXT,
    last_success_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(kind, locator)
)
"""


EVENTS_TABLE = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    editorial_tier TEXT NOT NULL DEFAULT 'pending',
    tier_reason TEXT NOT NULL DEFAULT '',
    curation_order INTEGER NOT NULL DEFAULT 9999,
    curation_status TEXT NOT NULL DEFAULT 'pending',
    primary_item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    curated_at TEXT
)
"""


ITEMS_TABLE = """
CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
    guid TEXT NOT NULL,
    canonical_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    display_title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    highlights_json TEXT NOT NULL DEFAULT '[]',
    summary_status TEXT NOT NULL DEFAULT 'pending',
    summary_error TEXT NOT NULL DEFAULT '',
    summary_version INTEGER NOT NULL DEFAULT 0,
    summarized_at TEXT,
    translated_content TEXT NOT NULL DEFAULT '',
    translation_status TEXT NOT NULL DEFAULT 'pending',
    translation_error TEXT NOT NULL DEFAULT '',
    translation_version INTEGER NOT NULL DEFAULT 0,
    translated_at TEXT,
    UNIQUE(source_id, guid)
)
"""


BRIEFS_TABLE = """
CREATE TABLE briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    intro TEXT NOT NULL,
    event_ids_json TEXT NOT NULL,
    generated_at TEXT NOT NULL
)
"""


FEEDBACK_TABLE = """
CREATE TABLE feedback (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(event_id, action)
) WITHOUT ROWID
"""


CONNECTOR_CREDENTIALS_TABLE = """
CREATE TABLE connector_credentials (
    connector TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_validated_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


TABLE_DEFINITIONS = (
    SOURCES_TABLE,
    EVENTS_TABLE,
    ITEMS_TABLE,
    BRIEFS_TABLE,
    FEEDBACK_TABLE,
    CONNECTOR_CREDENTIALS_TABLE,
)

INDEX_DEFINITIONS = (
    "CREATE INDEX idx_items_event ON items(event_id)",
    "CREATE INDEX idx_items_summary ON items(summary_status, summarized_at)",
    "CREATE INDEX idx_items_translation ON items(translation_status, translated_at)",
    (
        "CREATE INDEX idx_events_reader ON events("
        "curation_status, editorial_tier, curation_order, last_seen_at DESC, id DESC)"
    ),
    "CREATE INDEX idx_events_curation ON events(curation_status, last_seen_at DESC, id DESC)",
)

TARGET_INDEX_NAMES = frozenset(
    {
        "idx_items_event",
        "idx_items_summary",
        "idx_items_translation",
        "idx_events_reader",
        "idx_events_curation",
    }
)

TARGET_FOREIGN_KEYS = {
    "items": {
        ("source_id", "sources", "CASCADE"),
        ("event_id", "events", "SET NULL"),
    },
    "events": {("primary_item_id", "items", "SET NULL")},
    "feedback": {("event_id", "events", "CASCADE")},
}

TARGET_COLUMNS = {
    "sources": {
        "id",
        "name",
        "kind",
        "locator",
        "feed_url",
        "is_official",
        "enabled",
        "archived",
        "poll_interval_minutes",
        "config_json",
        "health_status",
        "last_fetch_at",
        "last_success_at",
        "last_error",
        "created_at",
        "updated_at",
    },
    "events": {
        "id",
        "title",
        "summary",
        "editorial_tier",
        "tier_reason",
        "curation_order",
        "curation_status",
        "primary_item_id",
        "first_seen_at",
        "last_seen_at",
        "curated_at",
    },
    "items": {
        "id",
        "source_id",
        "event_id",
        "guid",
        "canonical_url",
        "title",
        "display_title",
        "content",
        "author",
        "published_at",
        "fetched_at",
        "content_hash",
        "summary",
        "highlights_json",
        "summary_status",
        "summary_error",
        "summary_version",
        "summarized_at",
        "translated_content",
        "translation_status",
        "translation_error",
        "translation_version",
        "translated_at",
    },
    "briefs": {"id", "brief_date", "title", "intro", "event_ids_json", "generated_at"},
    "feedback": {"event_id", "action", "created_at"},
    "connector_credentials": {
        "connector",
        "ciphertext",
        "fingerprint",
        "status",
        "last_validated_at",
        "last_error",
        "created_at",
        "updated_at",
    },
}


class SchemaVersionError(RuntimeError):
    """已有数据库不是当前程序支持的固定结构。"""


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return name in _table_names(conn)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    }


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _foreign_keys(conn: sqlite3.Connection, table: str) -> set[tuple[str, str, str]]:
    if not _table_exists(conn, table):
        return set()
    return {
        (str(row[3]), str(row[2]), str(row[6]).upper())
        for row in conn.execute(f"PRAGMA foreign_key_list({_quote_identifier(table)})").fetchall()
    }


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    if not _table_exists(conn, table):
        return ()
    rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    return tuple(str(row[1]) for row in sorted(rows, key=lambda row: int(row[5])) if int(row[5]))


def _schema_issues(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    tables = _table_names(conn)
    expected = set(TARGET_TABLES)
    missing = sorted(expected - tables)
    unknown = sorted(tables - expected)
    if missing:
        issues.append(f"缺少目标表：{', '.join(missing)}")
    if unknown:
        issues.append(f"存在非 v7 表：{', '.join(unknown)}")

    for table, expected_columns in TARGET_COLUMNS.items():
        actual = _columns(conn, table)
        if actual and actual != expected_columns:
            missing_columns = sorted(expected_columns - actual)
            extra_columns = sorted(actual - expected_columns)
            details: list[str] = []
            if missing_columns:
                details.append(f"缺少 {', '.join(missing_columns)}")
            if extra_columns:
                details.append(f"多出 {', '.join(extra_columns)}")
            issues.append(f"{table} 字段不匹配（{'；'.join(details)}）")

    missing_indexes = sorted(TARGET_INDEX_NAMES - _index_names(conn))
    if missing_indexes:
        issues.append(f"缺少目标索引：{', '.join(missing_indexes)}")
    for table, expected_foreign_keys in TARGET_FOREIGN_KEYS.items():
        if _table_exists(conn, table) and _foreign_keys(conn, table) != expected_foreign_keys:
            issues.append(f"{table} 外键定义不匹配")
    if _table_exists(conn, "feedback") and _primary_key_columns(conn, "feedback") != (
        "event_id",
        "action",
    ):
        issues.append("feedback 主键定义不匹配")
    return issues


def _create_target_schema(conn: sqlite3.Connection) -> None:
    for statement in TABLE_DEFINITIONS:
        conn.execute(statement)
    for statement in INDEX_DEFINITIONS:
        conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def initialize_runtime_schema(conn: sqlite3.Connection) -> None:
    """初始化空库，或验证已经是 v7 的既有数据库。"""

    tables = _table_names(conn)
    if not tables:
        _create_target_schema(conn)
        conn.commit()
        return

    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_version != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"当前程序只支持 SQLite v{SCHEMA_VERSION}，检测到 v{current_version}；"
            "历史结构不会被自动修改。"
        )
    issues = _schema_issues(conn)
    if issues:
        raise SchemaVersionError("当前数据库不是完整的 v7 结构：" + "；".join(issues))
