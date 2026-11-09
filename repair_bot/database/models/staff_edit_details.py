"""Дополнительный снимок деталей и инструментов в истории правок."""

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from repair_bot.database.models.base import Base


class StaffEditDetails(Base):
    """Не меняет старую таблицу правок и исходный результат ИИ."""

    __tablename__ = "staff_edit_details"
    edit_id: Mapped[int] = mapped_column(ForeignKey("repair_request_staff_edits.id", ondelete="CASCADE"), primary_key=True)
    parts: Mapped[str] = mapped_column(Text)
    tools: Mapped[str] = mapped_column(Text)
    edit = relationship("RepairRequestStaffEdit", back_populates="details")
