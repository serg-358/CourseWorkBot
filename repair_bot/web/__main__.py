"""Локальный запуск веб-кабинета через Uvicorn."""

import uvicorn


if __name__ == "__main__":
    uvicorn.run("repair_bot.web.app:app", host="127.0.0.1", port=8000)
