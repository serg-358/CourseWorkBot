"""Модель зарегистрированного пользователя."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from repair_bot.database.models.base import Base, utcnow

if TYPE_CHECKING:
    from repair_bot.database.models.repair_request import RepairRequest


class User(Base):
    """Зарегистрированный пользователь Telegram-бота."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="client")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    requests: Mapped[list["RepairRequest"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
