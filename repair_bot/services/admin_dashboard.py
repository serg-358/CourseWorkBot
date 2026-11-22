"""Единый расчёт и выгрузка статистики административного дашборда."""

from collections.abc import Iterable
from copy import copy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo


MOSCOW_TIME_ZONE = ZoneInfo("Europe/Moscow")
STATUSES = ("new", "in_progress", "completed", "cancelled")
PRIORITIES = ("low", "medium", "high")
COMPLAINT_CATEGORIES = ("quality", "timing", "price", "communication", "other")
STATUS_LABELS = {"new": "Новые", "in_progress": "В работе", "completed": "Завершены", "cancelled": "Отменены"}
PRIORITY_LABELS = {"low": "Обычные", "medium": "Срочные", "high": "Критические"}
COMPLAINT_LABELS = {"quality": "Качество", "timing": "Сроки", "price": "Стоимость", "communication": "Общение", "other": "Другое"}


@dataclass(frozen=True)
class DashboardData:
    """Подготовленные данные периода для ЛК, Telegram и выгрузки."""

    label: str
    period_start: date
    period_end: date
    request_count: int
    trash_count: int
    status_counts: dict[str, int]
    priority_counts: dict[str, int]
    rating_counts: dict[int, int]
    complaint_counts: dict[str, int]
    average_rating: float | None
    timeline_labels: list[str]
    timeline_counts: list[int]


def moscow_day(value: datetime) -> date:
    """Возвращает московский календарный день для UTC-времени из базы."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(MOSCOW_TIME_ZONE).date()


def dashboard_period(requests: Iterable, period: str = "7", *, start: date | None = None, end: date | None = None) -> tuple[date, date, str]:
    """Определяет календарные границы дашборда по московскому времени."""
    rows = list(requests)
    last_day = max((moscow_day(item.created_at) for item in rows), default=date.today())
    if start is not None and end is not None:
        start, end = min(start, end), max(start, end)
        return start, end, f"{start:%d.%m.%Y}–{end:%d.%m.%Y}"
    if period == "all":
        return min((moscow_day(item.created_at) for item in rows), default=last_day), last_day, "Всё время"
    days = 30 if period == "30" else 7
    return last_day - timedelta(days=days - 1), last_day, f"Последние {days} дней"


def dashboard_buckets(period_start: date, period_end: date) -> list[tuple[date, date, str]]:
    """Разбивает период максимум на семь непересекающихся интервалов."""
    bucket_days = max(1, -(-((period_end - period_start).days + 1) // 7))
    buckets: list[tuple[date, date, str]] = []
    bucket_start = period_start
    while bucket_start <= period_end:
        bucket_end = min(bucket_start + timedelta(days=bucket_days - 1), period_end)
        label = bucket_start.strftime("%d.%m") if bucket_start == bucket_end else f"{bucket_start:%d.%m}–{bucket_end:%d.%m}"
        buckets.append((bucket_start, bucket_end, label))
        bucket_start = bucket_end + timedelta(days=1)
    return buckets


def build_dashboard_data(requests: Iterable, ratings: Iterable, complaints: Iterable, period: str = "7", *, start: date | None = None, end: date | None = None) -> DashboardData:
    """Считает статистику, включая заявки из корзины, за один период."""
    request_rows, rating_rows, complaint_rows = list(requests), list(ratings), list(complaints)
    period_start, period_end, label = dashboard_period(request_rows, period, start=start, end=end)
    selected_requests = [item for item in request_rows if period_start <= moscow_day(item.created_at) <= period_end]
    selected_ratings = [item for item in rating_rows if period_start <= moscow_day(item.created_at) <= period_end]
    selected_complaints = [item for item in complaint_rows if period_start <= moscow_day(item.created_at) <= period_end]
    buckets = dashboard_buckets(period_start, period_end)
    return DashboardData(
        label=label,
        period_start=period_start,
        period_end=period_end,
        request_count=len(selected_requests),
        trash_count=sum(
            bool(getattr(item, "lifecycle", None) and item.lifecycle.deleted_at)
            for item in selected_requests
        ),
        status_counts={status: sum(item.status == status for item in selected_requests) for status in STATUSES},
        priority_counts={priority: sum(item.priority == priority for item in selected_requests) for priority in PRIORITIES},
        rating_counts={score: sum(item.score == score for item in selected_ratings) for score in range(1, 6)},
        complaint_counts={category: sum(item.category == category for item in selected_complaints) for category in COMPLAINT_CATEGORIES},
        average_rating=(sum(item.score for item in selected_ratings) / len(selected_ratings) if selected_ratings else None),
        timeline_labels=[label for _start, _end, label in buckets],
        timeline_counts=[sum(start_day <= moscow_day(item.created_at) <= end_day for item in selected_requests) for start_day, end_day, _label in buckets],
    )


def dashboard_message(data: DashboardData, *, users_total: int, clients: int, masters: int, admins: int) -> str:
    """Формирует подробную, но компактную сводку для Telegram."""
    timeline = "\n".join(f"• {label}: {count}" for label, count in zip(data.timeline_labels, data.timeline_counts, strict=True))
    ratings = " · ".join(f"{score}★: {count}" for score, count in data.rating_counts.items())
    complaints = " · ".join(f"{COMPLAINT_LABELS[category]}: {count}" for category, count in data.complaint_counts.items())
    return (
        f"📊 <b>Дашборд мастерской</b> · {data.label}\n🗓 {data.period_start:%d.%m.%Y}–{data.period_end:%d.%m.%Y}\n\n"
        f"👥 Пользователи: {users_total} (клиенты: {clients}, мастера: {masters}, администраторы: {admins})\n\n"
        f"🧾 Заявки: {data.request_count}\n   Новые: {data.status_counts['new']} · В работе: {data.status_counts['in_progress']}\n"
        f"   Завершены: {data.status_counts['completed']} · Отменены: {data.status_counts['cancelled']}\n"
        f"   В корзине: {data.trash_count}\n   Обычные: {data.priority_counts['low']} · Срочные: {data.priority_counts['medium']} · Критические: {data.priority_counts['high']}\n\n"
        f"📈 <b>Заявки по интервалам</b>\n{timeline}\n\n⭐ Средняя оценка: {f'{data.average_rating:.1f}' if data.average_rating is not None else '—'}\n"
        f"   {ratings}\n⚠️ Жалобы: {sum(data.complaint_counts.values())}\n   {complaints}"
    )


def build_dashboard_xlsx(data: DashboardData) -> bytes:
    """Создаёт XLSX-выгрузку тех же показателей, что видит администратор."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    summary = workbook.active
    summary.title = "Сводка"
    for row in (("Дашборд мастерской",), ("Период", f"{data.period_start:%d.%m.%Y}–{data.period_end:%d.%m.%Y}"), ("Заявки", data.request_count), ("В корзине", data.trash_count), ("Средняя оценка", data.average_rating)):
        summary.append(row)
    summary["A1"].font = Font(bold=True, size=14)
    summary.column_dimensions["A"].width, summary.column_dimensions["B"].width = 24, 22
    _append_table(workbook, "Заявки по интервалам", ("Интервал", "Количество"), zip(data.timeline_labels, data.timeline_counts, strict=True))
    _append_table(workbook, "Статусы", ("Статус", "Количество"), ((STATUS_LABELS[key], value) for key, value in data.status_counts.items()))
    _append_table(workbook, "Срочность", ("Срочность", "Количество"), ((PRIORITY_LABELS[key], value) for key, value in data.priority_counts.items()))
    _append_table(workbook, "Оценки", ("Оценка", "Количество"), data.rating_counts.items())
    _append_table(workbook, "Жалобы", ("Категория", "Количество"), ((COMPLAINT_LABELS[key], value) for key, value in data.complaint_counts.items()))
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _append_table(workbook, title: str, headers: tuple[str, str], rows: Iterable[tuple[object, object]]) -> None:
    sheet = workbook.create_sheet(title)
    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))
    for cell in sheet[1]:
        font = copy(cell.font)
        font.bold = True
        cell.font = font
    sheet.column_dimensions["A"].width, sheet.column_dimensions["B"].width = 28, 16
