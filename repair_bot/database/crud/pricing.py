"""CRUD-операции для тарифов срочности."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repair_bot.database.models import UrgencyTariff

DEFAULT_URGENCY_TARIFFS = (
    ("ordinary", "Обычная", 0, 1),
    ("urgent", "Срочная", 50_000, 2),
    ("critical", "Критическая", 100_000, 3),
)


async def get_urgency_tariffs(db: AsyncSession) -> list[UrgencyTariff]:
    """Возвращает тарифы, добавляя согласованные значения для пустой таблицы."""
    result = await db.execute(
        select(UrgencyTariff).order_by(UrgencyTariff.display_order)
    )
    tariffs = list(result.scalars().all())
    if tariffs:
        return [tariff for tariff in tariffs if tariff.is_active]

    tariffs = [
        UrgencyTariff(
            code=code,
            label=label,
            surcharge_kopecks=surcharge,
            display_order=display_order,
        )
        for code, label, surcharge, display_order in DEFAULT_URGENCY_TARIFFS
    ]
    db.add_all(tariffs)
    await db.commit()
    return tariffs


async def get_urgency_tariff(
    db: AsyncSession, code: str
) -> UrgencyTariff | None:
    """Возвращает активный тариф по постоянному коду."""
    await get_urgency_tariffs(db)
    result = await db.execute(
        select(UrgencyTariff).where(
            UrgencyTariff.code == code, UrgencyTariff.is_active.is_(True)
        )
    )
    return result.scalar_one_or_none()


async def update_urgency_tariff(
    db: AsyncSession, code: str, *, label: str, surcharge_kopecks: int
) -> UrgencyTariff | None:
    """Обновляет название и доплату, не меняя снимки старых заявок."""
    if surcharge_kopecks < 0:
        raise ValueError("Доплата не может быть отрицательной.")
    tariff = await get_urgency_tariff(db, code)
    if tariff is None:
        return None
    tariff.label = label.strip()
    tariff.surcharge_kopecks = surcharge_kopecks
    await db.commit()
    await db.refresh(tariff)
    return tariff
