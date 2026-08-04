from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.storage.database import Database
from app.storage.migrations import (
    SCHEMA_VERSION,
    TARGET_COLUMNS,
    TARGET_INDEX_NAMES,
    SchemaVersionError,
)


class SchemaTests(unittest.TestCase):
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
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            finally:
                connection.close()

    def test_existing_v7_database_starts_without_rebuilding_tables(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "current.db"
            database = Database(path)
            database.initialize()
            with database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO sources (
                        name, kind, locator, feed_url, created_at, updated_at
                    ) VALUES ('OpenAI', 'rss', 'openai', 'https://example.test/feed', 'now', 'now')
                    """
                )

            database.initialize()
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
            finally:
                connection.close()

    def test_legacy_database_is_rejected_without_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE legacy_items (id INTEGER PRIMARY KEY)")
                connection.execute("PRAGMA user_version = 6")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(SchemaVersionError, "只支持 SQLite v7"):
                Database(path).initialize()

            check = sqlite3.connect(path)
            try:
                self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertIsNotNone(
                    check.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'legacy_items'"
                    ).fetchone()
                )
            finally:
                check.close()

    def test_incomplete_v7_database_is_rejected_without_auto_repair(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "incomplete.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY)")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(SchemaVersionError, "不是完整的 v7 结构"):
                Database(path).initialize()
