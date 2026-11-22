"""Агрегация и PNG-визуализация дашборда для Telegram-администратора."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont


STATUSES = ("new", "in_progress", "completed", "cancelled")
PRIORITIES = ("low", "medium", "high")
COMPLAINT_CATEGORIES = ("quality", "timing", "price", "communication", "other")


@dataclass(frozen=True)
class DashboardData:
    """Подготовленные данные периода для текста и графика дашборда."""

    label: str
    request_count: int
    status_counts: dict[str, int]
    priority_counts: dict[str, int]
    rating_counts: dict[int, int]
    complaint_counts: dict[str, int]
    average_rating: float | None
    timeline_labels: list[str]
    timeline_counts: list[int]


def build_dashboard_data(requests: Iterable, ratings: Iterable, complaints: Iterable, period: str) -> DashboardData:
    """Фильтрует данные периода и возвращает компактную сводку для Telegram."""
    request_rows = list(requests)
    rating_rows = list(ratings)
    complaint_rows = list(complaints)
    last_day = max((item.created_at.date() for item in request_rows), default=date.today())
    period_days = {"7": 7, "30": 30}.get(period)
    first_day = last_day - timedelta(days=period_days - 1) if period_days else None
    selected_requests = [
        item for item in request_rows
        if first_day is None or first_day <= item.created_at.date() <= last_day
    ]
    selected_ratings = [
        item for item in rating_rows
        if first_day is None or first_day <= item.created_at.date() <= last_day
    ]
    selected_complaints = [
        item for item in complaint_rows
        if first_day is None or first_day <= item.created_at.date() <= last_day
    ]
    timeline_labels, timeline_counts = _request_timeline(selected_requests, first_day or min(
        (item.created_at.date() for item in selected_requests), default=last_day
    ), last_day)
    return DashboardData(
        label={"7": "Последние 7 дней", "30": "Последние 30 дней", "all": "Всё время"}.get(period, "Всё время"),
        request_count=len(selected_requests),
        status_counts={status: sum(item.status == status for item in selected_requests) for status in STATUSES},
        priority_counts={priority: sum(item.priority == priority for item in selected_requests) for priority in PRIORITIES},
        rating_counts={score: sum(item.score == score for item in selected_ratings) for score in range(1, 6)},
        complaint_counts={category: sum(item.category == category for item in selected_complaints) for category in COMPLAINT_CATEGORIES},
        average_rating=(sum(item.score for item in selected_ratings) / len(selected_ratings) if selected_ratings else None),
        timeline_labels=timeline_labels,
        timeline_counts=timeline_counts,
    )


def render_dashboard_chart(data: DashboardData) -> bytes:
    """Рисует компактный PNG-график, совместимый с отправкой как Telegram photo."""
    image = Image.new("RGB", (1200, 720), "#0b0f14")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    title_font = ImageFont.truetype("DejaVuSans.ttf", 28) if _has_dejavu() else font
    colors = ("#78a6ff", "#3ddc97", "#b18cff", "#ff8a80", "#ffc857")
    draw.text((48, 36), "Дашборд: данные из БД", fill="#edf2f7", font=title_font)
    draw.text((48, 82), data.label, fill="#9eabb9", font=font)
    _panel(draw, (40, 120, 565, 410), "ЗАЯВКИ ПО ПЕРИОДАМ")
    _bar_chart(draw, (72, 170, 525, 365), data.timeline_counts, data.timeline_labels, colors[0])
    _panel(draw, (595, 120, 1160, 410), "СТАТУСЫ ЗАЯВОК")
    _donut(draw, (635, 170, 835, 370), list(data.status_counts.values()), colors[:4])
    _legend(draw, 870, 185, ("Новые", "В работе", "Завершены", "Отменены"), list(data.status_counts.values()), colors[:4])
    _panel(draw, (40, 440, 565, 680), "ОЦЕНКИ: ОТ 1 ДО 5")
    _bar_chart(draw, (72, 490, 525, 635), list(data.rating_counts.values()), [str(score) for score in range(1, 6)], colors[4])
    _panel(draw, (595, 440, 1160, 680), "ЖАЛОБЫ ПО КАТЕГОРИЯМ")
    _horizontal_bars(
        draw,
        (635, 485, 1120, 640),
        list(data.complaint_counts.values()),
        ("Качество", "Сроки", "Стоимость", "Общение", "Другое"),
        colors[3],
    )
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _has_dejavu() -> bool:
    try:
        ImageFont.truetype("DejaVuSans.ttf", 28)
    except OSError:
        return False
    return True


def _panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str) -> None:
    draw.rounded_rectangle(box, radius=22, fill="#121923")
    draw.text((box[0] + 28, box[1] + 22), title, fill="#9eabb9", font=ImageFont.load_default())


def _bar_chart(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    values: list[int],
    labels: list[str],
    color: str,
) -> None:
    left, top, right, bottom = box
    maximum = max(values, default=1) or 1
    draw.line((left, top, left, bottom), fill="#52606d", width=1)
    draw.line((left, bottom, right, bottom), fill="#52606d", width=1)
    width = (right - left) / max(len(values), 1)
    for index, value in enumerate(values):
        height = (bottom - top - 24) * value / maximum
        x1 = int(left + index * width + 12)
        x2 = int(left + (index + 1) * width - 12)
        y1 = int(bottom - height)
        if value:
            draw.rectangle((x1, y1, x2, bottom - 1), fill=color)
        draw.text((x1, max(top, y1 - 14)), str(value), fill="#edf2f7", font=ImageFont.load_default())
        draw.text((x1, bottom + 5), labels[index], fill="#9eabb9", font=ImageFont.load_default())


def _horizontal_bars(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    values: list[int],
    labels: tuple[str, ...],
    color: str,
) -> None:
    left, top, right, bottom = box
    maximum = max(values, default=1) or 1
    height = (bottom - top) / max(len(values), 1)
    for index, (value, label) in enumerate(zip(values, labels, strict=True)):
        y1 = int(top + index * height + 6)
        y2 = int(top + (index + 1) * height - 6)
        draw.rounded_rectangle((left, y1, right, y2), radius=7, fill="#202b38")
        if value:
            draw.rounded_rectangle((left, y1, int(left + (right - left) * value / maximum), y2), radius=7, fill=color)
        draw.text((left + 8, y1 + 3), f"{label}: {value}", fill="#edf2f7", font=ImageFont.load_default())


def _donut(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], values: list[int], colors: tuple[str, ...]) -> None:
    total = sum(values)
    if not total:
        draw.ellipse(box, fill="#202b38")
        draw.ellipse((box[0] + 45, box[1] + 45, box[2] - 45, box[3] - 45), fill="#121923")
        return
    start = -90
    for value, color in zip(values, colors, strict=True):
        angle = 360 * value / total
        draw.pieslice(box, start, start + angle, fill=color)
        start += angle
    draw.ellipse((box[0] + 45, box[1] + 45, box[2] - 45, box[3] - 45), fill="#121923")


def _legend(draw: ImageDraw.ImageDraw, x: int, y: int, labels: tuple[str, ...], values: list[int], colors: tuple[str, ...]) -> None:
    font = ImageFont.load_default()
    for index, (label, value, color) in enumerate(zip(labels, values, colors, strict=True)):
        row_y = y + index * 42
        draw.ellipse((x, row_y, x + 14, row_y + 14), fill=color)
        draw.text((x + 26, row_y), f"{label}: {value}", fill="#edf2f7", font=font)


def _request_timeline(requests: list, first_day: date, last_day: date) -> tuple[list[str], list[int]]:
    """Сжимает произвольный период максимум до семи читаемых столбцов."""
    span = (last_day - first_day).days + 1
    bucket_days = max(1, -(-span // 7))
    labels, counts = [], []
    bucket_start = first_day
    while bucket_start <= last_day:
        bucket_end = min(bucket_start + timedelta(days=bucket_days - 1), last_day)
        labels.append(
            bucket_start.strftime("%d.%m")
            if bucket_start == bucket_end
            else f"{bucket_start:%d.%m}-{bucket_end:%d.%m}"
        )
        counts.append(sum(bucket_start <= item.created_at.date() <= bucket_end for item in requests))
        bucket_start = bucket_end + timedelta(days=1)
    return labels, counts
