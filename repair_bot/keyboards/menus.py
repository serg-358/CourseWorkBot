"""Клавиатуры меню и диалогов."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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
            rows.append(
                [
                    InlineKeyboardButton(
                        text="↩️ Вернуться в панель администратора",
                        callback_data="view:admin",
                    )
                ]
            )
        elif role == "admin":
            rows.extend(
                [
                    [InlineKeyboardButton(text="🛠 Открыть панель мастера", callback_data="view:master")],
                    [InlineKeyboardButton(text="👀 Посмотреть меню клиента", callback_data="view:client")],
                ]
            )
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
