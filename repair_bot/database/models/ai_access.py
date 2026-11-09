"""Квоты AI, попытки анализа и аудит оплаченных разрешений."""

from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from repair_bot.database.models.base import Base, utcnow


class AiWallet(Base):
    """Telegram ID сохраняет лимит даже при удалении и повторной регистрации."""
    __tablename__ = "ai_wallets"
    telegram_id: Mapped[str] = mapped_column(String, primary_key=True)
    free_used: Mapped[bool] = mapped_column(Boolean, default=False)
    credits: Mapped[int] = mapped_column(Integer, default=0)


class AiUsage(Base):
    __tablename__ = "ai_usages"
    request_id: Mapped[int] = mapped_column(ForeignKey("repair_requests.id", ondelete="CASCADE"), primary_key=True)
    actor_telegram_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String(32), default="payment_required")
    state: Mapped[str] = mapped_column(String(16), default="blocked")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AiCreditGrant(Base):
    __tablename__ = "ai_credit_grants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[str] = mapped_column(String, index=True)
    actor_name: Mapped[str] = mapped_column(String(200))
    credits: Mapped[int] = mapped_column(Integer)
    amount_kopecks: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(32))
    reference: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
