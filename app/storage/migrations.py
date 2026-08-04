"""SQLite v9 结构，以及显式迁移的安全校验。

普通应用启动只初始化空数据库或接受已经是目标版本的数据库。已有数据库的
重建只能由 ``python -m app.migrate`` 执行，避免部署服务启动时悄悄删列。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable


SCHEMA_VERSION = 9

TARGET_TABLES = (
    "sources",
    "events",
    "items",
    "briefs",
    "feedback",
    "connector_credentials",
    "app_settings",
)

LEGACY_TABLES = (
    "sources",
    "events",
    "items",
    "fetch_runs",
    "event_items",
    "curation_runs",
    "briefs",
    "feedback",
    "connector_credentials",
    "app_settings",
    "analyses",
)


SOURCES_TABLE = """
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    locator TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    is_official INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 60,
    next_fetch_at TEXT,
    last_new_item_count INTEGER NOT NULL DEFAULT 0,
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
    media_json TEXT NOT NULL DEFAULT '[]',
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


APP_SETTINGS_TABLE = """
CREATE TABLE app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
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
    APP_SETTINGS_TABLE,
)

INDEX_DEFINITIONS = (
    "CREATE INDEX idx_items_event ON items(event_id)",
    "CREATE INDEX idx_items_summary ON items(summary_status, summarized_at)",
    "CREATE INDEX idx_items_translation ON items(translation_status, translated_at)",
    "CREATE INDEX idx_sources_due ON sources(enabled, archived, next_fetch_at)",
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
        "idx_sources_due",
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
        "description",
        "kind",
        "locator",
        "feed_url",
        "is_official",
        "enabled",
        "archived",
        "poll_interval_minutes",
        "next_fetch_at",
        "last_new_item_count",
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
        "media_json",
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
    "app_settings": {"key", "value", "updated_at"},
}

# v8 到 v9 只有 sources.description 一个带默认值的新字段，可在运行时安全补齐。
V8_TARGET_COLUMNS = {
    **TARGET_COLUMNS,
    "sources": TARGET_COLUMNS["sources"] - {"description"},
}


class MigrationRequiredError(RuntimeError):
    """已有数据库必须先通过维护命令显式迁移。"""


class MigrationPreflightError(RuntimeError):
    """旧数据库未通过安全预检，不能自动重建。"""


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """维护命令使用的只读预检结果。"""

    current_version: int
    target_version: int
    is_empty: bool
    is_current: bool
    row_counts: dict[str, int]
    feedback_target_rows: int
    discarded_rows: dict[str, int]
    raw_json_bytes: int
    fallback_url_rows: int
    brief_missing_event_references: int
    issues: tuple[str, ...]

    @property
    def can_apply(self) -> bool:
        return not self.issues


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _index_names(conn: sqlite3.Connection) -> set[str]:
    """返回显式命名的索引；SQLite 的 UNIQUE 自动索引不在其中。"""

    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
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


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table)}").fetchone()[0])


def _scalar(conn: sqlite3.Connection, sql: str, values: Iterable[object] = ()) -> int:
    row = conn.execute(sql, tuple(values)).fetchone()
    return int(row[0] or 0) if row else 0


def _brief_missing_event_reference_count(conn: sqlite3.Connection) -> int:
    """返回日报中引用不到现有事件的数量。

    ``event_ids_json`` 是小型日报的有序列表，不使用外键表。旧库中偶发的
    已删除事件引用可以在迁移时安全移除；JSON 本身无法解析时由调用方阻断迁移。
    """

    if not {"briefs", "events"} <= _table_names(conn):
        return 0
    if "event_ids_json" not in _columns(conn, "briefs"):
        return 0
    return _scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM briefs b
        JOIN json_each(b.event_ids_json) ids
        LEFT JOIN events e ON e.id = CAST(ids.value AS INTEGER)
        WHERE e.id IS NULL
        """,
    )


def _schema_issues(
    conn: sqlite3.Connection,
    *,
    target_columns: dict[str, set[str]] = TARGET_COLUMNS,
    schema_label: str = f"v{SCHEMA_VERSION}",
) -> list[str]:
    issues: list[str] = []
    tables = _table_names(conn)
    expected = set(TARGET_TABLES)
    missing = sorted(expected - tables)
    unknown = sorted(tables - expected)
    if missing:
        issues.append(f"缺少目标表：{', '.join(missing)}")
    if unknown:
        issues.append(f"存在非 {schema_label} 表：{', '.join(unknown)}")
    for table, expected_columns in target_columns.items():
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


def _legacy_relation_issues(conn: sqlite3.Connection) -> list[str]:
    """删除旧关联表前，先验证事件归属关系。"""

    issues: list[str] = []
    tables = _table_names(conn)
    if not {"sources", "items", "events"} <= tables:
        return ["旧数据库缺少 sources、items 或 events，无法安全迁移"]

    item_columns = _columns(conn, "items")
    event_columns = _columns(conn, "events")
    if not {"id", "source_id", "event_id"} <= item_columns:
        issues.append("items 缺少 id、source_id 或 event_id，无法确定事件归属")
    if not {"id", "primary_item_id"} <= event_columns:
        issues.append("events 缺少 id 或 primary_item_id，无法校验主条目")
    if issues:
        return issues

    if "event_items" in tables:
        event_item_columns = _columns(conn, "event_items")
        if {"event_id", "item_id"} <= event_item_columns:
            missing_link = _scalar(
                conn,
                """
                SELECT COUNT(*) FROM items i
                WHERE i.event_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM event_items ei
                    WHERE ei.event_id = i.event_id AND ei.item_id = i.id
                  )
                """,
            )
            conflicting_link = _scalar(
                conn,
                """
                SELECT COUNT(*) FROM event_items ei
                LEFT JOIN items i ON i.id = ei.item_id
                WHERE i.id IS NULL OR i.event_id IS NULL OR i.event_id <> ei.event_id
                """,
            )
            multi_event_item = _scalar(
                conn,
                """
                SELECT COUNT(*) FROM (
                    SELECT item_id FROM event_items GROUP BY item_id HAVING COUNT(*) > 1
                )
                """,
            )
            if missing_link:
                issues.append(f"有 {missing_link} 条 items.event_id 未映射到 event_items")
            if conflicting_link:
                issues.append(f"有 {conflicting_link} 条 event_items 与 items.event_id 不一致")
            if multi_event_item:
                issues.append(f"有 {multi_event_item} 条内容属于多个旧事件，无法自动选择保留关系")
        else:
            issues.append("event_items 缺少 event_id 或 item_id，无法校验重复关系")

    item_without_source = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM items i
        WHERE NOT EXISTS (SELECT 1 FROM sources s WHERE s.id = i.source_id)
        """,
    )
    item_without_event = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM items i
        WHERE i.event_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM events e WHERE e.id = i.event_id)
        """,
    )
    primary_invalid = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM events e
        WHERE e.primary_item_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM items i
            WHERE i.id = e.primary_item_id AND i.event_id = e.id
          )
        """,
    )
    if item_without_source:
        issues.append(f"有 {item_without_source} 条内容缺少来源")
    if item_without_event:
        issues.append(f"有 {item_without_event} 条内容指向不存在的事件")
    if primary_invalid:
        issues.append(f"有 {primary_invalid} 个事件的主条目无效或不属于该事件")

    if "feedback" in tables:
        feedback_columns = _columns(conn, "feedback")
        if {"event_id", "action", "created_at"} <= feedback_columns:
            no_event = _scalar(conn, "SELECT COUNT(*) FROM feedback WHERE event_id IS NULL")
            invalid_event = _scalar(
                conn,
                """
                SELECT COUNT(*) FROM feedback f
                WHERE f.event_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM events e WHERE e.id = f.event_id)
                """,
            )
            invalid_action = _scalar(
                conn,
                "SELECT COUNT(*) FROM feedback WHERE TRIM(COALESCE(action, '')) = ''",
            )
            if no_event:
                issues.append(f"有 {no_event} 条反馈不属于事件，不能安全迁移")
            if invalid_event:
                issues.append(f"有 {invalid_event} 条反馈指向不存在的事件")
            if invalid_action:
                issues.append(f"有 {invalid_action} 条反馈缺少 action")
        elif _count(conn, "feedback"):
            issues.append("feedback 缺少迁移所需字段")

    return issues


def inspect_migration(conn: sqlite3.Connection) -> MigrationReport:
    """只读返回迁移报告，绝不修改被检查的数据库。"""

    tables = _table_names(conn)
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    is_empty = not tables
    if is_empty:
        return MigrationReport(
            current_version=current_version,
            target_version=SCHEMA_VERSION,
            is_empty=True,
            is_current=False,
            row_counts={table: 0 for table in TARGET_TABLES},
            feedback_target_rows=0,
            discarded_rows={},
            raw_json_bytes=0,
            fallback_url_rows=0,
            brief_missing_event_references=0,
            issues=(),
        )

    row_counts = {table: _count(conn, table) for table in TARGET_TABLES}
    issues: list[str] = []
    integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
    if integrity != ["ok"]:
        issues.append(f"integrity_check 失败：{'；'.join(integrity[:3])}")
    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        issues.append(f"foreign_key_check 发现 {len(foreign_key_errors)} 条问题")

    brief_missing_event_references = 0
    try:
        brief_missing_event_references = _brief_missing_event_reference_count(conn)
    except sqlite3.DatabaseError:
        issues.append("日报 event_ids_json 不是可解析的 JSON")

    if current_version > SCHEMA_VERSION:
        issues.append(
            f"数据库版本 {current_version} 高于当前程序支持的 {SCHEMA_VERSION}，不能降级迁移"
        )

    if current_version == SCHEMA_VERSION:
        issues.extend(_schema_issues(conn))
        if brief_missing_event_references:
            issues.append(
                f"日报引用了 {brief_missing_event_references} 个不存在的事件；"
                f"当前 v{SCHEMA_VERSION} 数据库不自动修改数据"
            )
        return MigrationReport(
            current_version=current_version,
            target_version=SCHEMA_VERSION,
            is_empty=False,
            is_current=not issues,
            row_counts=row_counts,
            feedback_target_rows=row_counts["feedback"],
            discarded_rows={},
            raw_json_bytes=0,
            fallback_url_rows=0,
            brief_missing_event_references=brief_missing_event_references,
            issues=tuple(issues),
        )

    allowed_legacy = set(LEGACY_TABLES)
    unknown_tables = sorted(tables - allowed_legacy)
    if unknown_tables:
        issues.append(f"存在未识别的业务表：{', '.join(unknown_tables)}")
    issues.extend(_legacy_relation_issues(conn))

    feedback_target_rows = 0
    if "feedback" in tables and {"event_id", "action"} <= _columns(conn, "feedback"):
        feedback_target_rows = _scalar(
            conn,
            "SELECT COUNT(*) FROM (SELECT event_id, action FROM feedback GROUP BY event_id, action)",
        )

    discarded_rows = {
        table: _count(conn, table)
        for table in ("fetch_runs", "curation_runs", "event_items", "analyses")
        if table in tables
    }
    raw_json_bytes = 0
    if "items" in tables and "raw_json" in _columns(conn, "items"):
        raw_json_bytes = _scalar(conn, "SELECT SUM(LENGTH(raw_json)) FROM items")
    fallback_url_rows = 0
    if "sources" in tables and "fallback_url" in _columns(conn, "sources"):
        fallback_url_rows = _scalar(
            conn,
            "SELECT COUNT(*) FROM sources WHERE COALESCE(fallback_url, '') <> ''",
        )

    return MigrationReport(
        current_version=current_version,
        target_version=SCHEMA_VERSION,
        is_empty=False,
        is_current=False,
        row_counts=row_counts,
        feedback_target_rows=feedback_target_rows,
        discarded_rows=discarded_rows,
        raw_json_bytes=raw_json_bytes,
        fallback_url_rows=fallback_url_rows,
        brief_missing_event_references=brief_missing_event_references,
        issues=tuple(issues),
    )


def _create_target_schema(conn: sqlite3.Connection, *, include_indexes: bool = True) -> None:
    for statement in TABLE_DEFINITIONS:
        conn.execute(statement)
    if include_indexes:
        for statement in INDEX_DEFINITIONS:
            conn.execute(statement)
    _ensure_default_fetch_policy(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _ensure_default_fetch_policy(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value, updated_at)
        VALUES ('global_fetch_interval_minutes', '60', CURRENT_TIMESTAMP)
        """
    )


def _upgrade_v8_source_description(conn: sqlite3.Connection) -> bool:
    """仅对完整 v8 库追加简介列，避免常规 Compose 部署被无损改表阻断。"""

    if _schema_issues(conn, target_columns=V8_TARGET_COLUMNS, schema_label="v8"):
        return False
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE sources ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def initialize_runtime_schema(conn: sqlite3.Connection) -> None:
    """初始化新的 v9 数据库；仅自动追加 v8 的无损简介字段。"""

    tables = _table_names(conn)
    if not tables:
        _create_target_schema(conn)
        conn.commit()
        return

    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_version == 8 and _upgrade_v8_source_description(conn):
        current_version = SCHEMA_VERSION
    if current_version != SCHEMA_VERSION:
        raise MigrationRequiredError(
            "检测到旧版 SQLite 结构；请停止 web 和 worker 后运行 "
            "`docker compose --profile maintenance run --rm migrate --check`，"
            "再运行 `... migrate --apply`。"
        )
    issues = _schema_issues(conn)
    if issues:
        raise MigrationRequiredError(f"当前数据库不是完整的 v{SCHEMA_VERSION} 结构：" + "；".join(issues))


def apply_migrations(conn: sqlite3.Connection) -> None:
    """为旧导入路径保留的兼容名称。

    它刻意遵循运行时安全行为，绝不会隐式执行破坏性迁移。
    """

    initialize_runtime_schema(conn)


def _column_expression(
    columns: set[str], field: str, fallback: str, *, coalesce: bool = False
) -> str:
    if field not in columns:
        return fallback
    column = _quote_identifier(field)
    return f"COALESCE({column}, {fallback})" if coalesce else column


def _copy_columns(
    conn: sqlite3.Connection,
    *,
    target: str,
    legacy: str,
    fields: tuple[tuple[str, str, bool], ...],
    replace: bool = False,
) -> None:
    if not _table_exists(conn, legacy):
        return
    columns = _columns(conn, legacy)
    target_fields = ", ".join(_quote_identifier(field) for field, _, _ in fields)
    expressions = ", ".join(
        _column_expression(columns, field, fallback, coalesce=coalesce)
        for field, fallback, coalesce in fields
    )
    conflict = "OR REPLACE " if replace else ""
    conn.execute(
        f"INSERT {conflict}INTO {_quote_identifier(target)} ({target_fields}) "
        f"SELECT {expressions} FROM {_quote_identifier(legacy)}"
    )


SOURCE_COPY_FIELDS = (
    ("id", "NULL", False),
    ("name", "''", True),
    ("description", "''", True),
    ("kind", "'rss'", True),
    ("locator", "''", True),
    ("feed_url", "''", True),
    ("is_official", "0", True),
    ("enabled", "1", True),
    ("archived", "0", True),
    ("poll_interval_minutes", "60", True),
    ("next_fetch_at", "NULL", False),
    ("last_new_item_count", "0", True),
    ("config_json", "'{}'", True),
    ("health_status", "'unknown'", True),
    ("last_fetch_at", "NULL", False),
    ("last_success_at", "NULL", False),
    ("last_error", "''", True),
    ("created_at", "CURRENT_TIMESTAMP", True),
    ("updated_at", "CURRENT_TIMESTAMP", True),
)

EVENT_COPY_FIELDS = (
    ("id", "NULL", False),
    ("title", "''", True),
    ("summary", "''", True),
    ("editorial_tier", "'pending'", True),
    ("tier_reason", "''", True),
    ("curation_order", "9999", True),
    ("curation_status", "'pending'", True),
    ("primary_item_id", "NULL", False),
    ("first_seen_at", "CURRENT_TIMESTAMP", True),
    ("last_seen_at", "CURRENT_TIMESTAMP", True),
    ("curated_at", "NULL", False),
)

ITEM_COPY_FIELDS = (
    ("id", "NULL", False),
    ("source_id", "0", True),
    ("event_id", "NULL", False),
    ("guid", "''", True),
    ("canonical_url", "''", True),
    ("title", "''", True),
    ("display_title", "''", True),
    ("content", "''", True),
    ("author", "''", True),
    ("published_at", "NULL", False),
    ("fetched_at", "CURRENT_TIMESTAMP", True),
    ("content_hash", "''", True),
    ("summary", "''", True),
    ("highlights_json", "'[]'", True),
    ("summary_status", "'pending'", True),
    ("summary_error", "''", True),
    ("summary_version", "0", True),
    ("summarized_at", "NULL", False),
    ("translated_content", "''", True),
    ("translation_status", "'pending'", True),
    ("translation_error", "''", True),
    ("translation_version", "0", True),
    ("translated_at", "NULL", False),
    ("media_json", "'[]'", True),
)

BRIEF_COPY_FIELDS = (
    ("id", "NULL", False),
    ("brief_date", "''", True),
    ("title", "''", True),
    ("intro", "''", True),
    ("event_ids_json", "'[]'", True),
    ("generated_at", "CURRENT_TIMESTAMP", True),
)

CREDENTIAL_COPY_FIELDS = (
    ("connector", "''", True),
    ("ciphertext", "''", True),
    ("fingerprint", "''", True),
    ("status", "'unknown'", True),
    ("last_validated_at", "NULL", False),
    ("last_error", "''", True),
    ("created_at", "CURRENT_TIMESTAMP", True),
    ("updated_at", "CURRENT_TIMESTAMP", True),
)

APP_SETTINGS_COPY_FIELDS = (
    ("key", "''", True),
    ("value", "''", True),
    ("updated_at", "CURRENT_TIMESTAMP", True),
)


def _legacy_name(table: str) -> str:
    return f"__legacy_v7_{table}"


def _assert_preserved_counts(conn: sqlite3.Connection, report: MigrationReport) -> None:
    for table in ("sources", "events", "items", "briefs", "connector_credentials"):
        expected = report.row_counts.get(table, 0)
        actual = _count(conn, table)
        if actual != expected:
            raise MigrationPreflightError(
                f"迁移后 {table} 行数不一致：预期 {expected}，实际 {actual}"
            )
    feedback_rows = _count(conn, "feedback")
    if feedback_rows != report.feedback_target_rows:
        raise MigrationPreflightError(
            f"迁移后 feedback 行数不一致：预期 {report.feedback_target_rows}，实际 {feedback_rows}"
        )


def _repair_brief_missing_event_references(conn: sqlite3.Connection) -> int:
    """仅从旧日报移除不存在事件，保留日报行、有效 ID 与原有顺序。"""

    missing_count = _brief_missing_event_reference_count(conn)
    if not missing_count:
        return 0

    conn.execute(
        """
        UPDATE briefs AS b
        SET event_ids_json = COALESCE(
            (
                SELECT json_group_array(event_id)
                FROM (
                    SELECT e.id AS event_id
                    FROM json_each(b.event_ids_json) AS ids
                    JOIN events AS e ON e.id = CAST(ids.value AS INTEGER)
                    ORDER BY CAST(ids.key AS INTEGER)
                )
            ),
            '[]'
        )
        WHERE EXISTS (
            SELECT 1
            FROM json_each(b.event_ids_json) AS ids
            LEFT JOIN events AS e ON e.id = CAST(ids.value AS INTEGER)
            WHERE e.id IS NULL
        )
        """
    )

    remaining = _brief_missing_event_reference_count(conn)
    if remaining:
        raise MigrationPreflightError(f"日报无效事件引用清理后仍剩余 {remaining} 个")
    return missing_count


def apply_v9_migration(conn: sqlite3.Connection, report: MigrationReport | None = None) -> MigrationReport:
    """将已通过预检的旧结构重建为精简后的 v9。

    调用方必须在停掉 web/worker 后执行。这里使用单个显式事务；任何校验
    或复制失败都会回滚，不会留下半完成的表结构。
    """

    report = report or inspect_migration(conn)
    if not report.can_apply:
        raise MigrationPreflightError("预检未通过：" + "；".join(report.issues))
    if report.is_current:
        return report
    if report.is_empty:
        _create_target_schema(conn)
        conn.commit()
        return inspect_migration(conn)

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN EXCLUSIVE")
        for table in LEGACY_TABLES:
            if _table_exists(conn, table):
                legacy = _legacy_name(table)
                if _table_exists(conn, legacy):
                    raise MigrationPreflightError(f"发现遗留迁移表 {legacy}，请先人工检查")
                conn.execute(
                    f"ALTER TABLE {_quote_identifier(table)} RENAME TO {_quote_identifier(legacy)}"
                )

        # 旧的命名索引在遗留表真正删除前仍占用名称，先只建表；旧表删除
        # 后再创建 v9 索引，避免发生同名索引冲突。
        _create_target_schema(conn, include_indexes=False)
        _copy_columns(
            conn,
            target="sources",
            legacy=_legacy_name("sources"),
            fields=SOURCE_COPY_FIELDS,
        )
        _copy_columns(
            conn,
            target="events",
            legacy=_legacy_name("events"),
            fields=EVENT_COPY_FIELDS,
        )
        _copy_columns(
            conn,
            target="items",
            legacy=_legacy_name("items"),
            fields=ITEM_COPY_FIELDS,
        )
        _copy_columns(
            conn,
            target="briefs",
            legacy=_legacy_name("briefs"),
            fields=BRIEF_COPY_FIELDS,
        )
        repaired_brief_references = _repair_brief_missing_event_references(conn)
        if repaired_brief_references != report.brief_missing_event_references:
            raise MigrationPreflightError(
                "迁移期间日报无效事件引用数量发生变化，请重新执行预检"
            )
        _copy_columns(
            conn,
            target="connector_credentials",
            legacy=_legacy_name("connector_credentials"),
            fields=CREDENTIAL_COPY_FIELDS,
        )

        legacy_settings = _legacy_name("app_settings")
        if _table_exists(conn, legacy_settings):
            _copy_columns(
                conn,
                target="app_settings",
                legacy=legacy_settings,
                fields=APP_SETTINGS_COPY_FIELDS,
                # 保留已经设置过的全局抓取间隔，而不是被新库默认值覆盖。
                replace=True,
            )
        _ensure_default_fetch_policy(conn)

        legacy_feedback = _legacy_name("feedback")
        if _table_exists(conn, legacy_feedback):
            conn.execute(
                f"""
                INSERT INTO feedback (event_id, action, created_at)
                SELECT event_id, action, MAX(created_at)
                FROM {_quote_identifier(legacy_feedback)}
                WHERE event_id IS NOT NULL AND TRIM(COALESCE(action, '')) <> ''
                GROUP BY event_id, action
                """
            )

        _assert_preserved_counts(conn, report)

        for table in LEGACY_TABLES:
            legacy = _legacy_name(table)
            if _table_exists(conn, legacy):
                conn.execute(f"DROP TABLE {_quote_identifier(legacy)}")

        # 旧索引会随旧表一起删除；此处只创建目标结构真正需要的索引。
        for statement in INDEX_DEFINITIONS:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    verified = inspect_migration(conn)
    if not verified.is_current or not verified.can_apply:
        raise MigrationPreflightError("迁移后校验失败：" + "；".join(verified.issues))
    return verified


def apply_v8_migration(conn: sqlite3.Connection, report: MigrationReport | None = None) -> MigrationReport:
    """兼容旧导入名；当前维护命令会执行目标 v9 迁移。"""

    return apply_v9_migration(conn, report)


def apply_v7_migration(conn: sqlite3.Connection, report: MigrationReport | None = None) -> MigrationReport:
    """兼容更早的导入名；当前维护命令会执行目标 v9 迁移。"""

    return apply_v9_migration(conn, report)
