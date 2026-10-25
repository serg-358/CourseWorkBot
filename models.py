"""Модели базы данных Telegram-бота."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Возвращает UTC-время без tzinfo для совместимости с SQLite DateTime."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Базовый класс моделей SQLAlchemy."""


class User(Base):
    """Зарегистрированный пользователь бота."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="client")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    requests: Mapped[list[RepairRequest]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RepairRequest(Base):
    """Заявка пользователя на ремонт компьютерной техники."""

    __tablename__ = "repair_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    user: Mapped[User] = relationship(back_populates="requests")
    ai_assessment: Mapped[RepairRequestAiAssessment | None] = relationship(
        back_populates="request", cascade="all, delete-orphan", uselist=False
    )


class RepairRequestAiAssessment(Base):
    """Предварительная AI-оценка заявки, отделённая от основной записи."""

    __tablename__ = "repair_request_ai_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("repair_requests.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    photo_file_id: Mapped[str | None] = mapped_column(String, nullable=True)
    client_causes: Mapped[str] = mapped_column(Text, nullable=False)
    client_recommendations: Mapped[str] = mapped_column(Text, nullable=False)
    safety_note: Mapped[str] = mapped_column(Text, nullable=False)
    master_diagnosis: Mapped[str] = mapped_column(Text, nullable=False)
    parts: Mapped[str] = mapped_column(Text, nullable=False)
    tools: Mapped[str] = mapped_column(Text, nullable=False)
    catalog_item_codes: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_cost_kopecks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_price_kopecks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    request: Mapped[RepairRequest] = relationship(back_populates="ai_assessment")
