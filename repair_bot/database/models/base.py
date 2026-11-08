"""Объявление базового класса моделей базы данных."""

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """Возвращает UTC-время без tzinfo для совместимости с SQLite DateTime."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Базовый класс моделей SQLAlchemy."""
