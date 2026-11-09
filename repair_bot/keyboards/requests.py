"""Клавиатуры карточек и листания заявок."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def urgency_keyboard(tariffs) -> InlineKeyboardMarkup:
    """Показывает клиенту уровни срочности с отдельной доплатой."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="dialog:cancel")],
            *[
                [
                    InlineKeyboardButton(
                        text=f"{tariff.label} · {format_surcharge(tariff.surcharge_kopecks)}",
                        callback_data=f"urgency:choose:{tariff.code}",
                    )
                ]
                for tariff in tariffs
            ],
        ]
    )


def format_surcharge(kopecks: int) -> str:
    """Форматирует доплату в рублях без лишних нулей."""
    amount = f"{kopecks / 100:,.2f}".replace(",", " ").replace(".00", "")
    return f"+{amount} ₽" if kopecks else "без доплаты"


def request_status_keyboard(request_id: int, current_status: str) -> InlineKeyboardMarkup:
    """Возвращает кнопки смены статуса заявки для мастера и администратора."""
    statuses = (
        ("🔧 В работе", "in_progress"),
        ("✅ Завершена", "completed"),
        ("❌ Отменить", "cancelled"),
    )
    buttons = [
        InlineKeyboardButton(
            text=f"✓ {label}" if status == current_status else label,
            callback_data=f"request:status:{request_id}:{status}",
        )
        for label, status in statuses
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def request_navigation_keyboard(
    *,
    view: str,
    page: int,
    total_pages: int,
    menu: InlineKeyboardMarkup,
    request_id: int | None = None,
    current_status: str | None = None,
    has_photo: bool = False,
    can_append_description: bool = False,
    can_edit_assessment: bool = False,
    can_analyze: bool = False,
) -> InlineKeyboardMarkup:
    """Объединяет листание заявки, действия сотрудника и основное меню."""
    callback_prefix = f"requests:{view}"
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="← Предыдущая", callback_data=f"{callback_prefix}:{page - 1}")
        )
    navigation.append(
        InlineKeyboardButton(
            text=f"{page + 1} / {total_pages}",
            callback_data=f"requests:page-info:{page + 1}:{total_pages}",
        )
    )
    if page + 1 < total_pages:
        navigation.append(
            InlineKeyboardButton(text="Следующая →", callback_data=f"{callback_prefix}:{page + 1}")
        )

    rows = [navigation]
    if request_id is not None and can_analyze:
        rows.append([InlineKeyboardButton(text="🔎 Выполнить доступную AI-диагностику", callback_data=f"request:ai:{request_id}")])
    if request_id is not None and current_status is not None:
        rows.extend(request_status_keyboard(request_id, current_status).inline_keyboard)
    if request_id is not None and has_photo:
        rows.append(
            [InlineKeyboardButton(text="🖼️ Фото заявки", callback_data=f"request:photo:{request_id}")]
        )
    if request_id is not None and can_edit_assessment:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✏️ Исправить диагноз и цену",
                    callback_data=f"request:edit:{request_id}",
                )
            ]
        )
    if request_id is not None and can_append_description:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✏️ Дополнить описание",
                    callback_data=f"request:append:{request_id}",
                )
            ]
        )
    rows.extend(menu.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)
