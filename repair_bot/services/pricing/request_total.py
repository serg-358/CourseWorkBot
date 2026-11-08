"""Единый расчёт клиентского итога и подарочной скидки."""


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from repair_bot.database.models import RepairRequest


def certificate_discount(request: "RepairRequest", total: int) -> int:
    """Ограничивает списание номиналом сертификата и суммой заявки."""
    gift = getattr(request, "certificate_discount", None)
    return min(total, gift.amount_kopecks) if gift else 0


def final_total(request: "RepairRequest", repair_price: int, surcharge: int = 0) -> int:
    """Возвращает итог после скидки, не допускающий отрицательных сумм."""
    total = repair_price + surcharge
    return total - certificate_discount(request, total)
