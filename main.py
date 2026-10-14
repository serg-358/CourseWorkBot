"""Точка входа Telegram-бота."""

import asyncio
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from dotenv import load_dotenv

# Загружаем настройки до импорта модулей, читающих переменные окружения.
PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

from database import DATABASE_PATH, create_tables, engine
from fsm_storage import SQLiteFSMStorage


async def main() -> None:
    """Создаёт таблицы и запускает polling."""
    # Секретный токен и списки ролей берутся из окружения или локального .env.
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Укажите токен бота в переменной окружения BOT_TOKEN")

    # Импорт после load_dotenv нужен для корректного чтения списков ролей.
    from handlers.auth import router as auth_router

    await create_tables()
    storage = SQLiteFSMStorage(DATABASE_PATH)
    await storage.setup()
    bot = Bot(token=token)
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(auth_router)

    try:
        await dispatcher.start_polling(bot)
    finally:
        await storage.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
