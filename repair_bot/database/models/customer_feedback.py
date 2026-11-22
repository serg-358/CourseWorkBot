"""Сущности назначения мастера и обратной связи по ремонту."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from repair_bot.database.models.base import Base, utcnow


class RequestAssignment(Base):
    __tablename__ = "request_assignments"

    request_id: Mapped[int] = mapped_column(ForeignKey("repair_requests.id", ondelete="CASCADE"), primary_key=True)
    master_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    master_name: Mapped[str] = mapped_column(String(200), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class RequestCompletion(Base):
    __tablename__ = "request_completions"

    request_id: Mapped[int] = mapped_column(ForeignKey("repair_requests.id", ondelete="CASCADE"), primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)


class RepairRating(Base):
    __tablename__ = "repair_ratings"

    request_id: Mapped[int] = mapped_column(ForeignKey("repair_requests.id", ondelete="CASCADE"), primary_key=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)


class RepairComplaint(Base):
    __tablename__ = "repair_complaints"

    request_id: Mapped[int] = mapped_column(ForeignKey("repair_requests.id", ondelete="CASCADE"), primary_key=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    master_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    master_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
