from __future__ import annotations

import io
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.migrate import run as run_migration
from app.storage.database import Database
from app.storage.migrations import (
    SCHEMA_VERSION,
    TARGET_COLUMNS,
    TARGET_INDEX_NAMES,
    MigrationPreflightError,
    MigrationRequiredError,
    apply_v7_migration,
    apply_v10_migration,
    initialize_runtime_schema,
    inspect_migration,
)


def create_v6_database(
    path: Path,
    *,
    mismatched_event_items: bool = False,
    schema_version: int = 6,
    brief_event_ids_json: str = "[1]",
) -> None:
    """构造与已部署 v6 一致的最小数据库，不依赖真实用户数据。"""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, kind TEXT NOT NULL, locator TEXT NOT NULL,
            feed_url TEXT NOT NULL, is_official INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0,
            poll_interval_minutes INTEGER NOT NULL DEFAULT 60,
            fallback_url TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL DEFAULT '{}',
            health_status TEXT NOT NULL DEFAULT 'unknown', last_fetch_at TEXT,
            last_success_at TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(kind, locator)
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
            editorial_tier TEXT NOT NULL DEFAULT 'pending', tier_reason TEXT NOT NULL DEFAULT '',
            curation_order INTEGER NOT NULL DEFAULT 9999, curation_status TEXT NOT NULL DEFAULT 'pending',
            curated_at TEXT, curation_version INTEGER NOT NULL DEFAULT 1, primary_item_id INTEGER,
            source_count INTEGER NOT NULL DEFAULT 1, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
            guid TEXT NOT NULL, canonical_url TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,
            display_title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
            published_at TEXT, fetched_at TEXT NOT NULL, content_hash TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '', highlights_json TEXT NOT NULL DEFAULT '[]',
            summary_status TEXT NOT NULL DEFAULT 'pending', summary_error TEXT NOT NULL DEFAULT '',
            summary_version INTEGER NOT NULL DEFAULT 0, summarized_at TEXT,
            translated_content TEXT NOT NULL DEFAULT '', translation_status TEXT NOT NULL DEFAULT 'pending',
            translation_error TEXT NOT NULL DEFAULT '', translation_version INTEGER NOT NULL DEFAULT 0,
            translated_at TEXT, raw_json TEXT NOT NULL DEFAULT '{}', UNIQUE(source_id, guid)
        );
        CREATE TABLE fetch_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            new_item_count INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE event_items (
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            PRIMARY KEY(event_id, item_id)
        );
        CREATE TABLE curation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL,
            input_count INTEGER NOT NULL DEFAULT 0, event_count INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, finished_at TEXT
        );
        CREATE TABLE briefs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, brief_date TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL, intro TEXT NOT NULL, event_ids_json TEXT NOT NULL, generated_at TEXT NOT NULL
        );
        CREATE TABLE feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
            source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL, action TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE connector_credentials (
            connector TEXT PRIMARY KEY, ciphertext TEXT NOT NULL, fingerprint TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unknown', last_validated_at TEXT,
            last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_items_event ON items(event_id);
        CREATE INDEX idx_items_summary ON items(summary_status, summarized_at);
        CREATE INDEX idx_items_translation ON items(translation_status, translated_at);
        CREATE INDEX idx_events_curation ON events(curation_status, last_seen_at DESC);
        """
    )
    now = "2026-08-04T08:00:00+00:00"
    connection.execute(
        """
        INSERT INTO sources (
            id, name, kind, locator, feed_url, fallback_url, config_json, health_status,
            created_at, updated_at
        ) VALUES (1, 'OpenAI', 'x_rsshub', 'OpenAI', 'https://x.com/OpenAI',
                  'https://example.test/fallback', '{"x_user_id":"42"}', 'healthy', ?, ?)
        """,
        (now, now),
    )
    connection.execute(
        """
        INSERT INTO events (
            id, fingerprint, title, summary, editorial_tier, tier_reason, curation_order,
            curation_status, primary_item_id, source_count, first_seen_at, last_seen_at, created_at, updated_at
        ) VALUES (1, 'legacy-event', '旧事件', '旧摘要', 'important', '仍有价值', 1,
                  'complete', 1, 1, ?, ?, ?, ?)
        """,
        (now, now, now, now),
    )
    connection.execute(
        """
        INSERT INTO items (
            id, source_id, event_id, guid, canonical_url, title, display_title, content, author,
            fetched_at, content_hash, summary, highlights_json, summary_status, summary_version, raw_json
        ) VALUES (1, 1, 1, 'tweet-1', 'https://x.com/OpenAI/status/1', 'Old title', '中文标题',
                  '原始正文', 'OpenAI', ?, 'hash-1', '中文摘要', '["重点"]', 'complete', 2,
                  '{"unused":"raw payload"}')
        """,
        (now,),
    )
    event_id_for_link = 2 if mismatched_event_items else 1
    if mismatched_event_items:
        connection.execute(
            """
            INSERT INTO events (
                id, fingerprint, title, first_seen_at, last_seen_at, created_at, updated_at
            ) VALUES (2, 'other-event', '另一事件', ?, ?, ?, ?)
            """,
            (now, now, now, now),
        )
    connection.execute("INSERT INTO event_items (event_id, item_id) VALUES (?, 1)", (event_id_for_link,))
    connection.execute(
        "INSERT INTO fetch_runs (source_id, started_at, status) VALUES (1, ?, 'success')", (now,)
    )
    connection.execute(
        "INSERT INTO curation_runs (status, input_count, event_count, started_at) VALUES ('complete', 1, 1, ?)",
        (now,),
    )
    connection.execute(
        "INSERT INTO briefs (brief_date, title, intro, event_ids_json, generated_at) VALUES ('2026-08-04', '日报', '导语', ?, ?)",
        (brief_event_ids_json, now),
    )
    connection.executemany(
        "INSERT INTO feedback (event_id, source_id, action, created_at) VALUES (1, 1, 'read', ?)",
        [("2026-08-04T08:01:00+00:00",), ("2026-08-04T09:01:00+00:00",)],
    )
    connection.execute(
        "INSERT INTO feedback (event_id, source_id, action, created_at) VALUES (1, 1, 'not_interested', ?)",
        (now,),
    )
    connection.execute(
        """
        INSERT INTO connector_credentials (
            connector, ciphertext, fingerprint, status, created_at, updated_at
        ) VALUES ('x_session', 'opaque-ciphertext-not-to-decrypt', 'abcd1234', 'valid', ?, ?)
        """,
        (now, now),
    )
    connection.execute(f"PRAGMA user_version = {int(schema_version)}")
    connection.commit()
    connection.close()


class MigrationTests(unittest.TestCase):
    def test_empty_database_initializes_complete_v7_schema(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty.db"
            Database(path).initialize()
            connection = sqlite3.connect(path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                indexes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(tables, set(TARGET_COLUMNS))
                self.assertTrue(TARGET_INDEX_NAMES <= indexes)
            finally:
                connection.close()

    def test_runtime_refuses_legacy_database_without_changing_its_schema(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            create_v6_database(path)
            with self.assertRaises(MigrationRequiredError):
                Database(path).initialize()
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'event_items'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_preflight_is_read_only_and_reports_discarded_legacy_data(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            create_v6_database(path)
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                report = inspect_migration(connection)
            finally:
                connection.close()
            self.assertTrue(report.can_apply)
            self.assertFalse(report.is_current)
            self.assertEqual(report.current_version, 6)
            self.assertEqual(report.discarded_rows["fetch_runs"], 1)
            self.assertEqual(report.discarded_rows["event_items"], 1)
            self.assertEqual(report.feedback_target_rows, 2)
            self.assertGreater(report.raw_json_bytes, 0)
            self.assertEqual(report.fallback_url_rows, 1)

            check = sqlite3.connect(path)
            try:
                self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(check.execute("SELECT COUNT(*) FROM event_items").fetchone()[0], 1)
            finally:
                check.close()

    def test_dangling_brief_references_are_repaired_without_discarding_the_brief(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            create_v6_database(path, brief_event_ids_json="[2, 999, 1]")
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute(
                    """
                    INSERT INTO events (
                        id, fingerprint, title, first_seen_at, last_seen_at, created_at, updated_at
                    ) VALUES (2, 'second-event', '第二个事件', ?, ?, ?, ?)
                    """,
                    ("2026-08-04T08:00:00+00:00",) * 4,
                )
                connection.commit()
                report = inspect_migration(connection)
                self.assertTrue(report.can_apply)
                self.assertEqual(report.brief_missing_event_references, 1)
                verified = apply_v7_migration(connection, report)
                self.assertTrue(verified.is_current)
            finally:
                connection.close()

            check = sqlite3.connect(path)
            try:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM briefs").fetchone()[0], 1)
                self.assertEqual(
                    check.execute("SELECT event_ids_json FROM briefs").fetchone()[0], "[2,1]"
                )
                self.assertEqual(check.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
            finally:
                check.close()

    def test_unparseable_brief_json_still_blocks_migration(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "broken-brief.db"
            create_v6_database(path, brief_event_ids_json="not valid json")
            connection = sqlite3.connect(path)
            try:
                report = inspect_migration(connection)
                self.assertFalse(report.can_apply)
                self.assertIn("日报 event_ids_json 不是可解析的 JSON", report.issues)
                with self.assertRaises(MigrationPreflightError):
                    apply_v7_migration(connection, report)
            finally:
                connection.close()

            check = sqlite3.connect(path)
            try:
                self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(
                    check.execute("SELECT event_ids_json FROM briefs").fetchone()[0], "not valid json"
                )
            finally:
                check.close()

    def test_v9_database_with_dangling_brief_references_is_not_mutated(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "current.db"
            create_v6_database(path)
            connection = sqlite3.connect(path)
            try:
                apply_v7_migration(connection)
                connection.execute("DROP TABLE daily_topic_events")
                connection.execute("DROP TABLE daily_topics")
                connection.execute("DROP TABLE weekly_topic_events")
                connection.execute("DROP TABLE weekly_topics")
                connection.execute("PRAGMA user_version = 9")
                connection.execute("UPDATE briefs SET event_ids_json = '[999]'")
                connection.commit()
                report = inspect_migration(connection)
                self.assertFalse(report.is_current)
                self.assertFalse(report.can_apply)
                self.assertEqual(report.brief_missing_event_references, 1)
                self.assertTrue(any("请先修复当前 v9 数据后再升级" in issue for issue in report.issues))
            finally:
                connection.close()

    def test_runtime_refuses_v8_database_without_changing_it(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "v8.db"
            create_v6_database(path)
            connection = sqlite3.connect(path)
            try:
                apply_v7_migration(connection)
                connection.execute("ALTER TABLE sources DROP COLUMN description")
                connection.execute("PRAGMA user_version = 8")
                connection.commit()

                with self.assertRaises(MigrationRequiredError):
                    initialize_runtime_schema(connection)

                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 8)
                self.assertNotIn(
                    "description",
                    {row[1] for row in connection.execute("PRAGMA table_info(sources)")},
                )
            finally:
                connection.close()

    def test_complete_v9_database_adds_topic_tables_without_rebuilding_content(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "v9.db"
            create_v6_database(path)
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            try:
                apply_v7_migration(connection)
                connection.execute("DROP TABLE daily_topic_events")
                connection.execute("DROP TABLE daily_topics")
                connection.execute("DROP TABLE weekly_topic_events")
                connection.execute("DROP TABLE weekly_topics")
                connection.execute("PRAGMA user_version = 9")
                connection.commit()

                report = inspect_migration(connection)
                self.assertTrue(report.can_apply)
                self.assertEqual(report.current_version, 9)
                verified = apply_v10_migration(connection, report)
                self.assertTrue(verified.is_current)
                self.assertEqual(
                    connection.execute("SELECT title FROM events WHERE id = 1").fetchone()[0], "旧事件"
                )
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                self.assertIn("weekly_topics", tables)
                self.assertIn("weekly_topic_events", tables)
                self.assertIn("daily_topics", tables)
                self.assertIn("daily_topic_events", tables)
            finally:
                connection.close()

    def test_complete_v10_database_adds_daily_tables_and_preserves_weekly_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "v10.db"
            create_v6_database(path)
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            try:
                apply_v7_migration(connection)
                connection.execute(
                    """
                    INSERT INTO weekly_topics (week_start, display_name, created_at, updated_at)
                    VALUES ('2026-08-03', '旧周话题', '2026-08-05T00:00:00+00:00',
                            '2026-08-05T00:00:00+00:00')
                    """
                )
                weekly_topic_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                connection.execute(
                    """
                    INSERT INTO weekly_topic_events (week_start, topic_id, event_id)
                    VALUES ('2026-08-03', ?, 1)
                    """,
                    (weekly_topic_id,),
                )
                connection.execute("DROP TABLE daily_topic_events")
                connection.execute("DROP TABLE daily_topics")
                connection.execute("PRAGMA user_version = 10")
                connection.commit()

                report = inspect_migration(connection)
                self.assertTrue(report.can_apply)
                self.assertEqual(report.current_version, 10)
                verified = apply_v10_migration(connection, report)
                self.assertTrue(verified.is_current)
                self.assertEqual(
                    connection.execute("SELECT display_name FROM weekly_topics").fetchone()[0],
                    "旧周话题",
                )
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM daily_topics").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            finally:
                connection.close()

    def test_v5_legacy_database_uses_the_same_explicit_path(self) -> None:
        """当前已部署实例仍可能是 v5，不能只验证 v6 升级。"""

        with TemporaryDirectory() as directory:
            path = Path(directory) / "v5.db"
            create_v6_database(path, schema_version=5)
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            try:
                report = inspect_migration(connection)
                self.assertEqual(report.current_version, 5)
                self.assertTrue(report.can_apply)
                verified = apply_v7_migration(connection, report)
                self.assertTrue(verified.is_current)
            finally:
                connection.close()

    def test_migration_preserves_existing_global_fetch_policy(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            create_v6_database(path, schema_version=7)
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES ('global_fetch_interval_minutes', '45', '2026-08-04T08:00:00+00:00')
                """
            )
            connection.commit()
            connection.row_factory = sqlite3.Row
            try:
                verified = apply_v7_migration(connection)
                self.assertTrue(verified.is_current)
            finally:
                connection.close()

            check = sqlite3.connect(path)
            try:
                self.assertEqual(
                    check.execute(
                        "SELECT value FROM app_settings WHERE key = 'global_fetch_interval_minutes'"
                    ).fetchone()[0],
                    "45",
                )
            finally:
                check.close()

    def test_explicit_migration_preserves_required_data_and_removes_legacy_shape(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            create_v6_database(path)
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                report = apply_v7_migration(connection)
                self.assertTrue(report.is_current)
                self.assertTrue(report.can_apply)
            finally:
                connection.close()

            check = sqlite3.connect(path)
            try:
                tables = {
                    row[0]
                    for row in check.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                self.assertEqual(tables, set(TARGET_COLUMNS))
                for table, columns in TARGET_COLUMNS.items():
                    actual = {row[1] for row in check.execute(f"PRAGMA table_info({table})")}
                    self.assertEqual(actual, columns)
                self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(check.execute("SELECT event_id FROM items WHERE id = 1").fetchone()[0], 1)
                self.assertEqual(
                    check.execute("SELECT config_json FROM sources WHERE id = 1").fetchone()[0],
                    '{"x_user_id":"42"}',
                )
                self.assertEqual(
                    check.execute("SELECT ciphertext FROM connector_credentials WHERE connector = 'x_session'").fetchone()[0],
                    "opaque-ciphertext-not-to-decrypt",
                )
                feedback = check.execute(
                    "SELECT action, created_at FROM feedback WHERE event_id = 1 ORDER BY action"
                ).fetchall()
                self.assertEqual(feedback, [("not_interested", "2026-08-04T08:00:00+00:00"), ("read", "2026-08-04T09:01:00+00:00")])
                self.assertEqual(check.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                self.assertEqual(check.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                check.close()

    def test_mismatch_between_event_items_and_items_blocks_migration_without_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "broken.db"
            create_v6_database(path, mismatched_event_items=True)
            connection = sqlite3.connect(path)
            try:
                report = inspect_migration(connection)
                self.assertFalse(report.can_apply)
                self.assertTrue(any("event_items" in issue for issue in report.issues))
                with self.assertRaises(MigrationPreflightError):
                    apply_v7_migration(connection, report)
            finally:
                connection.close()
            check = sqlite3.connect(path)
            try:
                self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(check.execute("SELECT COUNT(*) FROM event_items").fetchone()[0], 1)
            finally:
                check.close()

    def test_cli_check_is_read_only_and_apply_creates_backup_before_migrating(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "legacy.db"
            backup_dir = root / "backups"
            create_v6_database(path, brief_event_ids_json="[1, 999]")

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_migration(["--check", "--database", str(path)]), 0)
            self.assertIn("需要迁移", output.getvalue())
            self.assertIn("日报将自动移除 1 个不存在的事件引用", output.getvalue())
            self.assertFalse(backup_dir.exists())

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    run_migration(
                        ["--apply", "--database", str(path), "--backup-dir", str(backup_dir)]
                    ),
                    0,
                )
            backups = list(backup_dir.glob("legacy-*.db"))
            self.assertEqual(len(backups), 1)
            migrated = sqlite3.connect(path)
            backup = sqlite3.connect(backups[0])
            try:
                self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertIsNotNone(
                    backup.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'event_items'"
                    ).fetchone()
                )
                self.assertEqual(
                    migrated.execute("SELECT event_ids_json FROM briefs").fetchone()[0], "[1]"
                )
            finally:
                migrated.close()
                backup.close()
