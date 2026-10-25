"""Создаёт таблицу AI-оценок в существующей SQLite-базе без потери данных."""

import asyncio
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from models import RepairRequestAiAssessment

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")
DEFAULT_DATABASE_PATH = PROJECT_DIR / "bot_database.db"
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(DEFAULT_DATABASE_PATH))).expanduser().resolve()


async def migrate_ai_assessments(database_path: Path = DATABASE_PATH) -> Path | None:
    """Создаёт недостающую таблицу и сохраняет копию существующей БД."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as connection:
            exists = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).has_table(
                    RepairRequestAiAssessment.__tablename__
                )
            )
            if exists:
                return None
            backup_path = None
            if database_path.exists() and database_path.stat().st_size:
                timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                backup_path = database_path.with_suffix(f".before-ai-{timestamp}.bak")
                shutil.copy2(database_path, backup_path)
            await connection.run_sync(
                lambda sync_connection: RepairRequestAiAssessment.__table__.create(
                    sync_connection, checkfirst=True
                )
            )
            return backup_path
    finally:
        await engine.dispose()


async def main() -> None:
    """Загружает конфигурацию и выполняет миграцию по явной команде."""
    backup_path = await migrate_ai_assessments()
    if backup_path:
        print(f"Миграция завершена. Резервная копия: {backup_path}")
    else:
        print("Миграция не требуется: таблица уже существует.")


if __name__ == "__main__":
    asyncio.run(main())
