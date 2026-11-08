"""Модель заявки на ремонт."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from repair_bot.database.models.base import Base, utcnow

if TYPE_CHECKING:
    from repair_bot.database.models.ai_assessment import RepairRequestAiAssessment
    from repair_bot.database.models.repair_request_staff_edit import RepairRequestStaffEdit
    from repair_bot.database.models.request_urgency import RequestUrgency
    from repair_bot.database.models.user import User


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
    user: Mapped["User"] = relationship(back_populates="requests")
    ai_assessment: Mapped["RepairRequestAiAssessment | None"] = relationship(
        back_populates="request", cascade="all, delete-orphan", uselist=False
    )
    staff_edits: Mapped[list["RepairRequestStaffEdit"]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="RepairRequestStaffEdit.created_at",
    )
    urgency: Mapped["RequestUrgency | None"] = relationship(
        back_populates="request", cascade="all, delete-orphan", uselist=False
    )
