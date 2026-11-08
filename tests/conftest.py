from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from repair_bot.database.models import Base


@pytest_asyncio.fixture
async def db_session_factory(tmp_path):
    """Создаёт изолированную фабрику сессий для одного теста."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_session_factory) -> AsyncIterator[AsyncSession]:
    """Выдаёт тестовую сессию и закрывает ее после проверки."""
    async with db_session_factory() as session:
        yield session
