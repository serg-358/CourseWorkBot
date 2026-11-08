"""Учетные данные веб-кабинета и одноразовая привязка Telegram."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from repair_bot.database.models.base import Base, utcnow

if TYPE_CHECKING:
    from repair_bot.database.models.user import User


class WebsiteAccount(Base):
    """Email-аккаунт сайта, связанный с Telegram после подтверждения кодом."""

    __tablename__ = "website_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    user: Mapped["User | None"] = relationship(back_populates="website_account")
    link_codes: Mapped[list["TelegramLinkCode"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class TelegramLinkCode(Base):
    """Одноразовый хеш кода, которым аккаунт привязывается в Telegram-боте."""

    __tablename__ = "telegram_link_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("website_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    account: Mapped["WebsiteAccount"] = relationship(back_populates="link_codes")


class RequestAddress(Base):
    """Адрес выезда, сохраненный снимком только для конкретной заявки."""

    __tablename__ = "request_addresses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("repair_requests.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    address: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )


class SiteText(Base):
    """Ограниченный набор редактируемых администратором текстов сайта."""

    __tablename__ = "site_texts"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
