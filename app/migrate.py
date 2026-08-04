"""显式执行 NewsRSSHub SQLite 结构维护。

普通 Web/Worker 启动绝不会调用这里的重建逻辑。线上升级应先停止服务，
再通过 ``python -m app.migrate --check`` 和 ``--apply`` 执行。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

from app.config import get_settings
from app.storage.migrations import (
    MigrationPreflightError,
    MigrationReport,
    apply_v7_migration,
    inspect_migration,
)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    """用 SQLite URI 的只读模式打开既有数据库，不创建文件或 WAL。"""

    if not path.is_file():
        raise FileNotFoundError(f"数据库文件不存在：{path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=20)
    connection.row_factory = sqlite3.Row
    return connection


def _writable_connection(path: Path) -> sqlite3.Connection:
    """打开已存在数据库，迁移时不隐式创建新的线上数据文件。"""

    if not path.is_file():
        raise FileNotFoundError(f"数据库文件不存在：{path}")
    connection = sqlite3.connect(path, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 20000")
    return connection


def _format_report(path: Path, report: MigrationReport) -> str:
    """输出只包含结构和数量，绝不泄露 Cookie、密文或 API Key。"""

    lines = [
        f"数据库：{path}",
        f"结构版本：v{report.current_version} → v{report.target_version}",
        f"数据库状态：{'空库' if report.is_empty else ('已是目标结构' if report.is_current else '需要迁移')}",
        "保留行数：" + "，".join(f"{name}={count}" for name, count in report.row_counts.items()),
    ]
    if report.discarded_rows:
        lines.append(
            "本次移除的历史记录："
            + "，".join(f"{name}={count}" for name, count in report.discarded_rows.items())
        )
    if report.feedback_target_rows != report.row_counts.get("feedback", 0):
        lines.append(f"反馈记录将按事件和动作去重保留：{report.feedback_target_rows} 条")
    if report.raw_json_bytes:
        lines.append(f"不再持久化的 raw_json 约：{report.raw_json_bytes} 字节")
    if report.fallback_url_rows:
        lines.append(f"不再使用的备用链接：{report.fallback_url_rows} 条")
    if report.brief_missing_event_references:
        if report.current_version < report.target_version:
            lines.append(
                "日报将自动移除 "
                f"{report.brief_missing_event_references} 个不存在的事件引用"
                "（保留日报和其余有效事件引用）"
            )
        else:
            lines.append(
                f"日报存在 {report.brief_missing_event_references} 个不存在的事件引用"
            )
    if report.issues:
        lines.append("预检问题：")
        lines.extend(f"- {issue}" for issue in report.issues)
    else:
        lines.append("预检：通过")
    return "\n".join(lines)


def _backup_database(source: sqlite3.Connection, source_path: Path, backup_dir: Path | None) -> Path:
    """通过 backup API 创建一致性备份，避免手工复制 WAL 中的 .db 文件。"""

    destination_dir = (backup_dir or source_path.parent / "backups").resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = destination_dir / f"{source_path.stem}-{timestamp}{source_path.suffix}"
    backup = sqlite3.connect(destination)
    try:
        source.backup(backup)
        integrity = [str(row[0]) for row in backup.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            raise MigrationPreflightError("备份完整性校验失败：" + "；".join(integrity[:3]))
    finally:
        backup.close()
    return destination


def _path_from_args(raw_path: str | None) -> Path:
    if raw_path:
        return Path(raw_path).expanduser().resolve()
    return get_settings().database_path.resolve()


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NewsRSSHub SQLite 安全迁移")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="只读预检，不修改数据库")
    action.add_argument("--apply", action="store_true", help="备份后执行 v7 迁移")
    parser.add_argument("--database", help="数据库文件路径；默认读取 config.yml")
    parser.add_argument("--backup-dir", help="备份目录；默认是数据库目录下的 backups")
    args = parser.parse_args(argv)

    database_path = _path_from_args(args.database)
    backup_dir = Path(args.backup_dir).expanduser().resolve() if args.backup_dir else None

    if args.check:
        try:
            connection = _readonly_connection(database_path)
            try:
                report = inspect_migration(connection)
            finally:
                connection.close()
        except (OSError, sqlite3.DatabaseError) as exc:
            print(f"预检失败：{exc}", file=sys.stderr)
            return 2
        print(_format_report(database_path, report))
        return 0 if report.can_apply else 2

    try:
        connection = _writable_connection(database_path)
        try:
            report = inspect_migration(connection)
            print(_format_report(database_path, report))
            if not report.can_apply:
                print("迁移未执行：请先处理预检问题。", file=sys.stderr)
                return 2
            if report.is_current:
                print("无需迁移：数据库已经是 v7，未创建备份也未修改数据。")
                return 0

            backup_path = _backup_database(connection, database_path, backup_dir)
            print(f"已创建 SQLite 一致性备份：{backup_path}")

            # 备份完成后重新预检，避免在等待期间数据库状态发生变化。
            refreshed = inspect_migration(connection)
            if not refreshed.can_apply:
                print(_format_report(database_path, refreshed), file=sys.stderr)
                print("迁移未执行：备份后预检未通过。", file=sys.stderr)
                return 2
            verified = apply_v7_migration(connection, refreshed)
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError, MigrationPreflightError) as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 2

    print(_format_report(database_path, verified))
    print("迁移完成：请启动 web 和 worker，并完成一次抓取与页面验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
