"""Клавиатуры Telegram-бота."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)


def main_menu_keyboard(
    role: str = "client",
    *,
    demo_role: str | None = None,
    allow_role_switch: bool = False,
) -> InlineKeyboardMarkup:
    """Возвращает отдельное меню клиента, мастера или администратора."""
    if role == "admin":
        rows = [
            [
                InlineKeyboardButton(text="👥 Клиенты", callback_data="menu:users:client"),
                InlineKeyboardButton(text="🛠 Мастера", callback_data="menu:users:master"),
            ],
            [InlineKeyboardButton(text="🛡 Администраторы", callback_data="menu:admins")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="menu:statistics")],
            [InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile")],
        ]
    elif role == "master":
        rows = [
            [InlineKeyboardButton(text="🧾 Заявки на ремонт", callback_data="menu:all_requests")],
            [InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile")],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(text="🧾 Мои заявки", callback_data="menu:requests"),
                InlineKeyboardButton(text="➕ Создать заявку", callback_data="menu:create"),
            ],
            [InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile")],
        ]

    if allow_role_switch:
        if demo_role:
            rows.append([
                InlineKeyboardButton(
                    text="↩️ Вернуться в панель администратора",
                    callback_data="view:admin",
                )
            ])
        elif role == "admin":
            rows.extend([
                [InlineKeyboardButton(text="🛠 Открыть панель мастера", callback_data="view:master")],
                [InlineKeyboardButton(text="👀 Посмотреть меню клиента", callback_data="view:client")],
            ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dialog_keyboard(menu: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Оставляет отмену и разделы меню доступными во время диалога."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="dialog:cancel")],
            *menu.inline_keyboard,
        ]
    )


def photo_dialog_keyboard(menu: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Добавляет возможность завершить заявку без фотографии."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖼️ Пропустить фото", callback_data="ai:skip-photo")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="dialog:cancel")],
            *menu.inline_keyboard,
        ]
    )


def request_status_keyboard(request_id: int, current_status: str) -> InlineKeyboardMarkup:
    """Возвращает кнопки смены статуса заявки для мастера и администратора."""
    statuses = (("🔧 В работе", "in_progress"), ("✅ Завершена", "completed"), ("❌ Отменить", "cancelled"))
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
    if request_id is not None and current_status is not None:
        rows.extend(request_status_keyboard(request_id, current_status).inline_keyboard)
    if request_id is not None and has_photo:
        rows.append([InlineKeyboardButton(text="🖼️ Фото заявки", callback_data=f"request:photo:{request_id}")])
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
