from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.storage.migrations import initialize_runtime_schema


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _open_connection(self, *, enable_wal: bool) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=20, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 20000")
        if enable_wal:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def connect(self) -> sqlite3.Connection:
        return self._open_connection(enable_wal=True)

    def initialize(self) -> None:
        # 先检查结构，再切换 WAL。若发现旧库，本次启动除了打开连接外不对
        # 服务器数据库产生任何结构或日志模式变更。
        connection = self._open_connection(enable_wal=False)
        try:
            # 服务启动只允许初始化空库或使用 v9；仅允许 v8 无损追加账号简介列。
            initialize_runtime_schema(connection)
            connection.execute("PRAGMA journal_mode = WAL")
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived read connection and always release its file handle."""
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
