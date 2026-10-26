"""Точка входа Telegram-бота."""

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from dotenv import load_dotenv

# Загружаем настройки до импорта модулей, читающих переменные окружения.
PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

from database import DATABASE_PATH, create_tables, engine
from fsm_storage import SQLiteFSMStorage

logger = logging.getLogger(__name__)
POLLING_RETRY_DELAY_SECONDS = 5


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
        while True:
            try:
                await dispatcher.start_polling(bot)
                break
            except TelegramNetworkError:
                logger.exception(
                    "Не удалось подключиться к Telegram. Повтор через %s секунд.",
                    POLLING_RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(POLLING_RETRY_DELAY_SECONDS)
    finally:
        await storage.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
