"""Срочность и доплата, зафиксированные при создании заявки."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from repair_bot.database.models.base import Base, utcnow

if TYPE_CHECKING:
    from repair_bot.database.models.repair_request import RepairRequest


class RequestUrgency(Base):
    """Снимок выбранного тарифа, не меняющийся при последующей настройке цен."""

    __tablename__ = "request_urgencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("repair_requests.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    tariff_code: Mapped[str] = mapped_column(String(24), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    surcharge_kopecks: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    request: Mapped["RepairRequest"] = relationship(back_populates="urgency")
