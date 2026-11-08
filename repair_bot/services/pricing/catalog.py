"""Небольшой проверяемый каталог типовых работ и деталей в копейках."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogItem:
    code: str
    title: str
    cost_kopecks: int


CATALOG: dict[str, CatalogItem] = {
    "diagnostics": CatalogItem("diagnostics", "Диагностика", 50000),
    "os_reinstall": CatalogItem("os_reinstall", "Переустановка ОС", 80000),
    "ram_8gb": CatalogItem("ram_8gb", "Модуль оперативной памяти 8 ГБ", 250000),
    "ssd_500gb": CatalogItem("ssd_500gb", "SSD 500 ГБ", 350000),
    "power_supply": CatalogItem("power_supply", "Блок питания", 300000),
    "cooling_service": CatalogItem("cooling_service", "Чистка и обслуживание охлаждения", 70000),
}


def calculate_cost(codes: list[str]) -> tuple[list[CatalogItem], int | None, int | None]:
    """Возвращает позиции и цены; неизвестный код делает оценку стоимости невозможной."""
    if not codes or any(code not in CATALOG for code in codes):
        return [], None, None
    items = [CATALOG[code] for code in dict.fromkeys(codes)]
    cost = sum(item.cost_kopecks for item in items)
    return items, cost, (cost * 115 + 99) // 100
