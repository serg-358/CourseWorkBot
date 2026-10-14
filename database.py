"""Подключение к SQLite и управление сессиями."""

from collections.abc import AsyncGenerator
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models import Base

DEFAULT_DATABASE_PATH = Path(__file__).resolve().parent / "bot_database.db"
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(DEFAULT_DATABASE_PATH))).expanduser().resolve()
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Выдаёт сессию БД и гарантированно закрывает её после использования."""
    async with SessionLocal() as db:
        yield db


async def create_tables() -> None:
    """Создаёт таблицы, описанные в моделях."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
