"""Учёт выдачи, резервирования и погашения подарочных сертификатов."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from repair_bot.database.models.base import Base, utcnow


class GiftCertificate(Base):
    """Один сертификат за квалифицирующий ремонт, один — на следующий."""

    __tablename__ = "gift_certificates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_request_id: Mapped[int | None] = mapped_column(ForeignKey("repair_requests.id", ondelete="SET NULL"), unique=True)
    source_number: Mapped[int] = mapped_column(Integer)
    qualifying_total_kopecks: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    amount_kopecks: Mapped[int] = mapped_column(Integer, default=100_000)
    target_request_id: Mapped[int | None] = mapped_column(ForeignKey("repair_requests.id", ondelete="SET NULL"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
