"""Корзина, происхождение импорта и фото независимо от наличия AI-оценки."""

from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from repair_bot.database.models.base import Base, utcnow


class RequestLifecycle(Base):
    __tablename__ = "request_lifecycle"
    request_id: Mapped[int] = mapped_column(ForeignKey("repair_requests.id", ondelete="CASCADE"), primary_key=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by_name: Mapped[str] = mapped_column(String(200), default="")
    imported: Mapped[bool] = mapped_column(Boolean, default=False)
    external_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    provenance: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_file_id: Mapped[str | None] = mapped_column(String, nullable=True)


class RequestImportBatch(Base):
    __tablename__ = "request_import_batches"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
