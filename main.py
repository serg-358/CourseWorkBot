"""Совместимая точка запуска Telegram-бота."""

import asyncio

from repair_bot.bootstrap import main

if __name__ == "__main__":
    asyncio.run(main())
