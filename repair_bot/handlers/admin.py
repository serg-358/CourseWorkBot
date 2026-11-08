"""Обработчики раздела admin."""

import html
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from repair_bot.database.crud.requests import get_all_requests
from repair_bot.database.crud.users import get_all_users, get_user_by_telegram_id
from repair_bot.database.session import SessionLocal
from repair_bot.keyboards import main_menu_keyboard
from repair_bot.handlers.common import (
    _demo_view_roles,
    edit_callback_screen,
    is_admin_telegram_id,
    menu_for_user,
    role_for_telegram_id,
)
import logging
from aiogram import Router

router = Router(name="admin")
logger = logging.getLogger(__name__)

@router.callback_query(F.data.startswith("menu:users"))
async def show_users(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает администратору пользователей выбранной роли."""
    if callback.from_user is None or not is_admin_telegram_id(callback.from_user.id):
        await callback.answer("🔒 Этот раздел доступен только администратору.", show_alert=True)
        return
    await state.clear()

    async with SessionLocal() as db:
        admin = await get_user_by_telegram_id(db, str(callback.from_user.id))
        if admin is None or admin.role != "admin":
            users = None
        else:
            users = await get_all_users(db)

    if users is None:
        await callback.answer("🔒 Этот раздел доступен только администратору.", show_alert=True)
        return

    role_filter = (callback.data or "").rsplit(":", 1)[-1]
    if role_filter not in {"client", "master"}:
        role_filter = None  # Старые кнопки показывают полный список.
    if users is not None and role_filter:
        users = [user for user in users if user.role == role_filter]

    await callback.answer()
    if callback.message is None:
        return

    if not users:
        empty_labels = {"client": "📭 Клиентов пока нет.", "master": "📭 Мастеров пока нет."}
        title = {"client": "Клиенты", "master": "Мастера"}.get(role_filter, "Пользователи")
        text = f"👥 <b>{title}</b>\n\n{empty_labels.get(role_filter, 'Пользователей пока нет.')}"
    else:
        titles = {"client": "Клиенты", "master": "Мастера"}
        lines = [f"👥 <b>{titles.get(role_filter, 'Все пользователи')}</b> · {len(users)}"]
        for user in users[:20]:
            lines.append(
                f"\n\n👤 <b>{html.escape(user.full_name[:60])}</b> · {user.role}\n"
                f"☎️ {html.escape(user.phone[:20])}\n🆔 {html.escape(user.telegram_id[:24])}"
            )
        if len(users) > 20:
            lines.append(f"\n\nПоказаны первые 20 из {len(users)} пользователей.")
        text = "\n".join(lines)

    await edit_callback_screen(
        callback,
        text,
        reply_markup=menu_for_user(callback.from_user.id, "admin"),
        parse_mode="HTML",
    )

@router.callback_query(F.data == "menu:admins")
async def show_administrators(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает отдельный список аккаунтов с ролью администратора."""
    if callback.from_user is None or not is_admin_telegram_id(callback.from_user.id):
        await callback.answer("🔒 Этот раздел доступен только администратору.", show_alert=True)
        return
    await state.clear()

    async with SessionLocal() as db:
        current_user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        users = await get_all_users(db) if current_user and current_user.role == "admin" else None

    if users is None:
        await callback.answer("🔒 Нет прав для просмотра раздела.", show_alert=True)
        return

    admins = [user for user in users if user.role == "admin"]
    lines = [f"🛡 <b>Администраторы</b> · {len(admins)}"]
    lines.extend(f"\n\n👤 {html.escape(user.full_name)}" for user in admins)
    await callback.answer()
    if callback.message:
        await edit_callback_screen(
            callback,
            "\n".join(lines),
            reply_markup=menu_for_user(callback.from_user.id, "admin"),
            parse_mode="HTML",
        )

@router.callback_query(F.data == "menu:statistics")
async def show_statistics(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает доступную сейчас статистику по зарегистрированным пользователям."""
    if callback.from_user is None or not is_admin_telegram_id(callback.from_user.id):
        await callback.answer("🔒 Этот раздел доступен только администратору.", show_alert=True)
        return
    await state.clear()

    async with SessionLocal() as db:
        admin = await get_user_by_telegram_id(db, str(callback.from_user.id))
        if admin is None or admin.role != "admin":
            users = None
        else:
            users = await get_all_users(db)
            requests = await get_all_requests(db)

    if users is None:
        await callback.answer("🔒 Этот раздел доступен только администратору.", show_alert=True)
        return

    clients = sum(user.role == "client" for user in users)
    admins = sum(user.role == "admin" for user in users)
    statuses = {
        "new": sum(request.status == "new" for request in requests),
        "in_progress": sum(request.status == "in_progress" for request in requests),
        "completed": sum(request.status == "completed" for request in requests),
        "cancelled": sum(request.status == "cancelled" for request in requests),
    }
    await callback.answer()
    if callback.message:
        await edit_callback_screen(
            callback,
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Пользователи: {len(users)}\n"
            f"   Клиенты: {clients} · Мастера: {sum(user.role == 'master' for user in users)}\n"
            f"   Администраторы: {admins}\n\n"
            f"🧾 Заявки: {len(requests)}\n"
            f"   Новые: {statuses['new']} · В работе: {statuses['in_progress']}\n"
            f"   Завершены: {statuses['completed']} · Отменены: {statuses['cancelled']}",
            reply_markup=menu_for_user(callback.from_user.id, "admin"),
            parse_mode="HTML",
        )

@router.callback_query(F.data == "menu:profile")
async def show_profile(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает профиль и актуализирует роль администратора."""
    await callback.answer()
    if callback.from_user is None:
        return
    await state.clear()
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        if user:
            expected_role = role_for_telegram_id(callback.from_user.id)
        else:
            expected_role = None
        if user and user.role != expected_role:
            # Поддерживаем отображаемую роль в соответствии с allowlist Telegram ID.
            user.role = expected_role
            await db.commit()

    if user is None:
        if callback.message:
            await edit_callback_screen(
                callback,
                "📭 Профиль не найден. Нажмите /start для регистрации.",
                reply_markup=main_menu_keyboard(),
            )
        return

    if callback.message:
        await edit_callback_screen(
            callback,
            f"👤 <b>Ваш профиль</b>\n\n"
            f"Имя: {html.escape(user.full_name)}\nТелефон: {html.escape(user.phone)}\n"
            f"Роль в системе: <b>{html.escape(user.role)}</b>"
            + (
                f"\nРежим показа: {_demo_view_roles[callback.from_user.id]}"
                if callback.from_user.id in _demo_view_roles
                else ""
            ),
            reply_markup=menu_for_user(callback.from_user.id, user.role),
            parse_mode="HTML",
        )
