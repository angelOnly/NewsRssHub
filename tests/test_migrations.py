from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.storage.database import Database


class MigrationTests(unittest.TestCase):
    def test_legacy_content_scoring_columns_are_physically_removed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-content.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL, kind TEXT NOT NULL, locator TEXT NOT NULL,
                    feed_url TEXT NOT NULL, category TEXT NOT NULL, priority INTEGER NOT NULL,
                    is_official INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
                    archived INTEGER NOT NULL DEFAULT 0, poll_interval_minutes INTEGER NOT NULL DEFAULT 60,
                    fallback_url TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL DEFAULT '{}',
                    health_status TEXT NOT NULL DEFAULT 'unknown', last_fetch_at TEXT,
                    last_success_at TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(kind, locator)
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
                    why_matters TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
                    importance_score REAL NOT NULL DEFAULT 0, confidence TEXT NOT NULL DEFAULT '',
                    primary_item_id INTEGER, source_count INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    analysis_status TEXT NOT NULL DEFAULT 'pending', analysis_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
                    guid TEXT NOT NULL, canonical_url TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '', published_at TEXT,
                    fetched_at TEXT NOT NULL, relevance_score REAL NOT NULL DEFAULT 0,
                    tags_json TEXT NOT NULL DEFAULT '[]', blacklisted INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL DEFAULT '{}', UNIQUE(source_id, guid)
                );
                CREATE TABLE event_items (
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    PRIMARY KEY(event_id, item_id)
                );
                CREATE TABLE feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
                    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
                    action TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL
                );
                INSERT INTO sources (
                    name, kind, locator, feed_url, category, priority, created_at, updated_at
                ) VALUES ('source', 'rss', 'https://example.test/feed', 'https://example.test/feed', 'old', 9, 'now', 'now');
                INSERT INTO events (
                    fingerprint, title, summary, why_matters, importance_score,
                    first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES ('legacy-event', 'old event', 'old summary', 'old analysis', 99, 'now', 'now', 'now', 'now');
                INSERT INTO items (source_id, event_id, guid, title, fetched_at)
                VALUES (1, 1, 'item-1', 'old item', 'now');
                INSERT INTO event_items (event_id, item_id) VALUES (1, 1);
                INSERT INTO feedback (event_id, source_id, action, created_at)
                VALUES (1, 1, 'not_interested', 'now');
                INSERT INTO analyses (event_id, payload_json) VALUES (1, '{}');
                """
            )
            connection.commit()
            connection.close()

            Database(path).initialize()
            check = sqlite3.connect(path)
            item_columns = {row[1] for row in check.execute("PRAGMA table_info(items)")}
            event_columns = {row[1] for row in check.execute("PRAGMA table_info(events)")}
            event_item = check.execute("SELECT event_id, item_id FROM event_items").fetchone()
            feedback = check.execute("SELECT event_id, source_id FROM feedback").fetchone()
            analyses = check.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'analyses'"
            ).fetchone()
            foreign_key_errors = check.execute("PRAGMA foreign_key_check").fetchall()
            version = check.execute("PRAGMA user_version").fetchone()[0]
            check.close()

            self.assertFalse({"relevance_score", "tags_json", "blacklisted"} & item_columns)
            self.assertFalse(
                {
                    "why_matters",
                    "tags_json",
                    "importance_score",
                    "confidence",
                    "analysis_status",
                    "analysis_version",
                }
                & event_columns
            )
            self.assertEqual(event_item, (1, 1))
            self.assertEqual(feedback, (1, 1))
            self.assertIsNone(analyses)
            self.assertEqual(foreign_key_errors, [])
            self.assertGreaterEqual(version, 4)

    def test_legacy_source_category_and_priority_are_physically_removed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL, kind TEXT NOT NULL, locator TEXT NOT NULL,
                    feed_url TEXT NOT NULL, category TEXT NOT NULL, priority INTEGER NOT NULL,
                    is_official INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
                    archived INTEGER NOT NULL DEFAULT 0, poll_interval_minutes INTEGER NOT NULL DEFAULT 60,
                    fallback_url TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL DEFAULT '{}',
                    health_status TEXT NOT NULL DEFAULT 'unknown', last_fetch_at TEXT,
                    last_success_at TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(kind, locator)
                );
                CREATE TABLE items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    event_id INTEGER, guid TEXT NOT NULL, canonical_url TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
                    published_at TEXT, fetched_at TEXT NOT NULL, relevance_score REAL NOT NULL DEFAULT 0,
                    tags_json TEXT NOT NULL DEFAULT '[]', blacklisted INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL DEFAULT '{}', UNIQUE(source_id, guid)
                );
                CREATE TABLE fetch_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                    new_item_count INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO sources (
                    name, kind, locator, feed_url, category, priority, created_at, updated_at
                ) VALUES ('OpenAI', 'x_rsshub', 'OpenAI', 'https://x.com/OpenAI', '旧主题', 10, 'now', 'now');
                INSERT INTO items (source_id, guid, title, fetched_at) VALUES (1, 'tweet-1', '旧帖子', 'now');
                INSERT INTO fetch_runs (source_id, started_at, status) VALUES (1, 'now', 'success');
                """
            )
            connection.commit()
            connection.close()

            Database(path).initialize()
            check = sqlite3.connect(path)
            columns = {row[1] for row in check.execute("PRAGMA table_info(sources)")}
            source = check.execute("SELECT name, kind, locator FROM sources").fetchone()
            item_source = check.execute("SELECT source_id FROM items WHERE guid = 'tweet-1'").fetchone()
            fetch_source = check.execute("SELECT source_id FROM fetch_runs").fetchone()
            foreign_key_errors = check.execute("PRAGMA foreign_key_check").fetchall()
            version = check.execute("PRAGMA user_version").fetchone()[0]
            check.close()
            self.assertNotIn("category", columns)
            self.assertNotIn("priority", columns)
            self.assertEqual(source, ("OpenAI", "x_rsshub", "OpenAI"))
            self.assertEqual(item_source, (1,))
            self.assertEqual(fetch_source, (1,))
            self.assertEqual(foreign_key_errors, [])
            self.assertGreaterEqual(version, 3)
