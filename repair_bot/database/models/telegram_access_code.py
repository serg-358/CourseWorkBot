"""Одноразовые коды из Telegram и устойчивые к смене сессии лимиты попыток."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from repair_bot.database.models.base import Base, utcnow


class TelegramAccessCode(Base):
    """Хранит только HMAC кода, срок действия и отметку использования."""

    __tablename__ = "telegram_access_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LinkCodeRateLimit(Base):
    """Ограничивает число попыток аккаунтом и хешированным IP-адресом."""

    __tablename__ = "link_code_rate_limits"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
