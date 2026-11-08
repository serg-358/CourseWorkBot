"""Копирует данные SQLite в пустую PostgreSQL-БД с резервной копией."""

import asyncio
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from repair_bot.database.models import Base
from repair_bot.database.session import DEFAULT_DATABASE_PATH

BATCH_SIZE = 500


def normalize_postgres_url(url: str) -> str:
    """Указывает asyncpg-драйвер для распространенных форм URL PostgreSQL."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def backup_sqlite_database(source_path: Path) -> Path:
    """Создает согласованную копию SQLite через встроенный backup API."""
    if not source_path.is_file():
        raise FileNotFoundError(f"Не найден исходный файл SQLite: {source_path}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = source_path.with_name(f"{source_path.stem}.pre-postgres-{stamp}.db")
    with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as backup:
        source.backup(backup)
    shutil.copystat(source_path, backup_path)
    return backup_path


async def _table_names(connection) -> set[str]:
    return await connection.run_sync(
        lambda sync_connection: set(inspect(sync_connection).get_table_names())
    )


async def _count_rows(connection, table) -> int:
    result = await connection.execute(select(func.count()).select_from(table))
    return int(result.scalar_one())


async def migrate(
    source_path: Path,
    target_url: str,
    *,
    backup_path: Path | None = None,
) -> dict[str, int]:
    """Переносит существующие таблицы в пустую PostgreSQL и сверяет количества."""
    target_url = normalize_postgres_url(target_url)
    parsed_target = make_url(target_url)
    if parsed_target.get_backend_name() != "postgresql":
        raise ValueError("Целевая база должна быть PostgreSQL.")
    if not source_path.is_file():
        raise FileNotFoundError(f"Не найден исходный файл SQLite: {source_path}")

    if backup_path is None:
        backup_path = backup_sqlite_database(source_path)
    source_engine = create_async_engine(f"sqlite+aiosqlite:///{backup_path.as_posix()}")
    target_engine = create_async_engine(target_url)
    try:
        async with source_engine.connect() as source_connection:
            source_names = await _table_names(source_connection)
        metadata_names = set(Base.metadata.tables)
        target_existing: set[str]
        async with target_engine.begin() as target_connection:
            target_existing = await _table_names(target_connection)
            unknown = target_existing - metadata_names
            if unknown:
                raise RuntimeError(
                    "Целевая база содержит неизвестные таблицы: "
                    + ", ".join(sorted(unknown))
                )
            for table in Base.metadata.sorted_tables:
                if table.name in target_existing:
                    if await _count_rows(target_connection, table):
                        raise RuntimeError(
                            f"Целевая таблица {table.name} не пуста; перенос остановлен."
                        )
            await target_connection.run_sync(Base.metadata.create_all)

        counts: dict[str, int] = {}
        async with source_engine.connect() as source_connection:
            async with target_engine.begin() as target_connection:
                for table in Base.metadata.sorted_tables:
                    if table.name in source_names:
                        result = await source_connection.execute(select(table))
                        while rows := result.mappings().fetchmany(BATCH_SIZE):
                            await target_connection.execute(
                                table.insert(), [dict(row) for row in rows]
                            )
                    copied_count = await _count_rows(target_connection, table)
                    source_count = (
                        await _count_rows(source_connection, table)
                        if table.name in source_names
                        else 0
                    )
                    if copied_count != source_count:
                        raise RuntimeError(
                            f"Проверка {table.name}: SQLite={source_count}, "
                            f"PostgreSQL={copied_count}."
                        )
                    counts[table.name] = copied_count

                    integer_pk = next(
                        (
                            column
                            for column in table.primary_key.columns
                            if str(column.type).upper() == "INTEGER"
                        ),
                        None,
                    )
                    if integer_pk is not None:
                        await target_connection.execute(
                            text(
                                "SELECT setval(pg_get_serial_sequence(:table_name, "
                                ":column_name), GREATEST(COALESCE((SELECT MAX(\""
                                + integer_pk.name
                                + "\") FROM \""
                                + table.name
                                + "\"), 1), 1), EXISTS (SELECT 1 FROM \""
                                + table.name
                                + "\"))"
                            ),
                            {
                                "table_name": table.name,
                                "column_name": integer_pk.name,
                            },
                        )
        return counts
    finally:
        await source_engine.dispose()
        await target_engine.dispose()


async def main() -> None:
    """Запускает перенос и выводит только имена таблиц и число строк."""
    source_path = Path(
        os.getenv("SOURCE_SQLITE_PATH", os.getenv("DATABASE_PATH", str(DEFAULT_DATABASE_PATH)))
    ).expanduser().resolve()
    target_url = os.getenv("TARGET_DATABASE_URL", "").strip()
    if not target_url:
        raise RuntimeError("Укажите TARGET_DATABASE_URL для новой PostgreSQL-БД.")
    backup_path = backup_sqlite_database(source_path)
    counts = await migrate(source_path, target_url, backup_path=backup_path)
    print(
        json.dumps(
            {"backup": str(backup_path), "rows": counts}, ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
