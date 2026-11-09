"""Применение доверенной добавочной SQLite-миграции с бэкапом и проверкой данных."""

import argparse
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def apply_migration(database: Path, migration: Path, backup_dir: Path) -> Path:
    """Не создаёт пустую рабочую БД по ошибочному пути, не изменяет старые колонки."""
    database = database.resolve(strict=True)
    statements = []
    pending = ""
    for line in migration.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("--"):
            continue
        pending += line + "\n"
        if sqlite3.complete_statement(pending):
            sql = pending.strip()
            pending = ""
            if sql.upper() in {"BEGIN IMMEDIATE;", "COMMIT;"}:
                continue
            if not re.match(r"CREATE (?:TABLE|(?:UNIQUE )?INDEX) IF NOT EXISTS\s", sql, re.I):
                raise ValueError("Разрешены только добавочные CREATE ... IF NOT EXISTS.")
            statements.append(sql)
    if pending.strip() or not statements:
        raise ValueError("Неполная или пустая SQL-миграция.")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / (database.stem + "-migration-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".db")
    def quote(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'
    with sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=30) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Рабочая БД не прошла integrity_check.")
        with sqlite3.connect(backup) as copy:
            backup.chmod(0o600)
            db.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Резервная копия не прошла integrity_check.")
        db.execute("BEGIN IMMEDIATE")
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        def snapshot():
            return {name: (db.execute(f"SELECT COUNT(*) FROM {quote(name)}").fetchone()[0], db.execute(f"PRAGMA table_info({quote(name)})").fetchall()) for name in tables}
        before = snapshot()
        for statement in statements:
            db.execute(statement)
        if snapshot() != before or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Миграция изменила исходные строки/колонки или нарушила целостность.")
        db.commit()
    return backup


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("migration", type=Path)
    parser.add_argument("backup_dir", type=Path)
    args = parser.parse_args()
    backup = apply_migration(args.database, args.migration, args.backup_dir)
    print(f"Migration OK; original rows/columns preserved; backup: {backup.name}")
