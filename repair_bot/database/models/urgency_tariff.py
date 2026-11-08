"""Тарифы доплаты за срочность обслуживания."""

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from repair_bot.database.models.base import Base


class UrgencyTariff(Base):
    """Настраиваемый администратором тариф срочности."""

    __tablename__ = "urgency_tariffs"

    code: Mapped[str] = mapped_column(String(24), primary_key=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    surcharge_kopecks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
