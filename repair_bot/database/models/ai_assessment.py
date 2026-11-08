"""Модель предварительной AI-оценки заявки."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from repair_bot.database.models.base import Base, utcnow

if TYPE_CHECKING:
    from repair_bot.database.models.repair_request import RepairRequest


class RepairRequestAiAssessment(Base):
    """Предварительная оценка ИИ, отделённая от основной записи заявки."""

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
    request: Mapped["RepairRequest"] = relationship(back_populates="ai_assessment")
