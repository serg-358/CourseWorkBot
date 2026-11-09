"""Единый приоритет обслуживания, выбранный клиентом, без влияния AI."""

PRIORITY_BY_URGENCY = {"ordinary": "low", "urgent": "medium", "critical": "high"}


def selected_priority(request) -> str:
    """Для старых заявок тоже использует сохранённый выбор срочности."""
    urgency = getattr(request, "urgency", None)
    code = getattr(urgency, "tariff_code", None)
    return PRIORITY_BY_URGENCY.get(code, getattr(request, "priority", "low"))
