"""История исправлений диагноза и цены сотрудником."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from repair_bot.database.models.base import Base, utcnow

if TYPE_CHECKING:
    from repair_bot.database.models.repair_request import RepairRequest
    from repair_bot.database.models.user import User


class RepairRequestStaffEdit(Base):
    """Снимок подтверждённых диагноза и цены с автором изменения."""

    __tablename__ = "repair_request_staff_edits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("repair_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    editor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    editor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    diagnosis: Mapped[str] = mapped_column(Text, nullable=False)
    client_price_kopecks: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    request: Mapped["RepairRequest"] = relationship(back_populates="staff_edits")
    editor: Mapped["User | None"] = relationship(
        back_populates="request_edits", foreign_keys=[editor_user_id]
    )
