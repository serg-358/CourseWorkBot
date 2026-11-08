"""Постоянное FSM-хранилище на SQLite для Aiogram 3."""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiosqlite
from aiogram.fsm.storage.base import BaseStorage, StorageKey


class SQLiteFSMStorage(BaseStorage):
    """Сохраняет состояния и данные диалогов в отдельной таблице SQLite."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = str(database_path)
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(key: StorageKey) -> str:
        """Формирует уникальный ключ для пользователя и чата Telegram."""
        return json.dumps(
            [
                key.bot_id,
                key.chat_id,
                key.user_id,
                key.thread_id,
                key.business_connection_id,
                key.destiny,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    async def setup(self) -> None:
        """Создаёт таблицу хранения FSM при запуске приложения."""
        async with self._lock, aiosqlite.connect(self.database_path, timeout=30) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS fsm_storage (
                    storage_key TEXT PRIMARY KEY,
                    state TEXT NULL,
                    data TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            await db.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._lock, aiosqlite.connect(self.database_path, timeout=30) as db:
            cursor = await db.execute(
                "SELECT state FROM fsm_storage WHERE storage_key = ?", (self._key(key),)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_state(
        self, key: StorageKey, state: str | None = None
    ) -> None:
        state_name = state.state if hasattr(state, "state") else state
        async with self._lock, aiosqlite.connect(self.database_path, timeout=30) as db:
            await db.execute(
                """INSERT INTO fsm_storage (storage_key, state) VALUES (?, ?)
                   ON CONFLICT(storage_key) DO UPDATE SET state = excluded.state""",
                (self._key(key), state_name),
            )
            await db.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._lock, aiosqlite.connect(self.database_path, timeout=30) as db:
            cursor = await db.execute(
                "SELECT data FROM fsm_storage WHERE storage_key = ?", (self._key(key),)
            )
            row = await cursor.fetchone()
            if row is None:
                return {}
            try:
                data = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        serialized = json.dumps(dict(data), ensure_ascii=False)
        async with self._lock, aiosqlite.connect(self.database_path, timeout=30) as db:
            await db.execute(
                """INSERT INTO fsm_storage (storage_key, data) VALUES (?, ?)
                   ON CONFLICT(storage_key) DO UPDATE SET data = excluded.data""",
                (self._key(key), serialized),
            )
            await db.commit()

    async def close(self) -> None:
        """Соединения закрываются после каждой операции, поэтому cleanup не требуется."""
