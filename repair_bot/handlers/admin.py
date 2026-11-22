"""Обработчики раздела admin."""

import html
from math import ceil

from aiogram import F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from repair_bot.database.crud.feedback import list_complaints
from repair_bot.database.crud.requests import get_all_requests
from repair_bot.database.crud.users import get_all_users, get_user_by_telegram_id
from repair_bot.database.models import RepairRating
from repair_bot.database.session import SessionLocal
from repair_bot.keyboards import main_menu_keyboard
from repair_bot.services.admin_dashboard import build_dashboard_data, render_dashboard_chart
from repair_bot.handlers.common import (
    _demo_view_roles,
    edit_callback_screen,
    configured_role_for_telegram_id,
    menu_for_user,
)
import logging
from aiogram import Router

router = Router(name="admin")
logger = logging.getLogger(__name__)

COMPLAINTS_PER_PAGE = 5
COMPLAINT_LABELS = {
    "quality": "Качество",
    "timing": "Сроки",
    "price": "Стоимость",
    "communication": "Общение",
    "other": "Другое",
}
DASHBOARD_PERIODS = {"7", "30", "all"}


def dashboard_keyboard(telegram_id: int, period: str) -> InlineKeyboardMarkup:
    """Добавляет к меню администратора переключатель периода дашборда."""
    labels = (("7", "7 дней"), ("30", "30 дней"), ("all", "Всё время"))
    periods = [
        InlineKeyboardButton(
            text=f"✓ {label}" if value == period else label,
            callback_data=f"menu:dashboard:{value}",
        )
        for value, label in labels
    ]
    menu = menu_for_user(telegram_id, "admin")
    return InlineKeyboardMarkup(inline_keyboard=[periods, *menu.inline_keyboard])

@router.callback_query(F.data.startswith("menu:users"))
async def show_users(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает администратору пользователей выбранной роли."""
    if callback.from_user is None:
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
    if callback.from_user is None:
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

@router.callback_query(F.data.startswith("menu:dashboard"))
@router.callback_query(F.data == "menu:statistics")
async def show_dashboard(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает администратору сводку по пользователям, заявкам и отзывам."""
    if callback.from_user is None:
        await callback.answer("🔒 Этот раздел доступен только администратору.", show_alert=True)
        return
    previous_data = await state.get_data()
    await state.clear()
    period = (callback.data or "").rsplit(":", 1)[-1]
    period = period if period in DASHBOARD_PERIODS else "7"

    async with SessionLocal() as db:
        admin = await get_user_by_telegram_id(db, str(callback.from_user.id))
        if admin is None or admin.role != "admin":
            users = None
        else:
            users = await get_all_users(db)
            requests = await get_all_requests(db)
            complaints = await list_complaints(db)
            ratings = list((await db.scalars(select(RepairRating))).all())

    if users is None:
        await callback.answer("🔒 Этот раздел доступен только администратору.", show_alert=True)
        return

    dashboard = build_dashboard_data(requests, ratings, complaints, period)
    clients = sum(user.role == "client" for user in users)
    admins = sum(user.role == "admin" for user in users)
    await callback.answer()
    if callback.message:
        await edit_callback_screen(
            callback,
            f"📊 <b>Дашборд мастерской</b> · {dashboard.label}\n\n"
            f"👥 Пользователи: {len(users)}\n"
            f"   Клиенты: {clients} · Мастера: {sum(user.role == 'master' for user in users)}\n"
            f"   Администраторы: {admins}\n\n"
            f"🧾 Заявки: {dashboard.request_count}\n"
            f"   Новые: {dashboard.status_counts['new']} · В работе: {dashboard.status_counts['in_progress']}\n"
            f"   Завершены: {dashboard.status_counts['completed']} · Отменены: {dashboard.status_counts['cancelled']}\n"
            f"   Обычные: {dashboard.priority_counts['low']} · Срочные: {dashboard.priority_counts['medium']} · Критические: {dashboard.priority_counts['high']}\n\n"
            f"⭐ Средняя оценка: {f'{dashboard.average_rating:.1f}' if dashboard.average_rating is not None else '—'}\n"
            f"⚠️ Жалобы: {sum(dashboard.complaint_counts.values())}",
            reply_markup=dashboard_keyboard(callback.from_user.id, period),
            parse_mode="HTML",
        )
        previous_chat_id = previous_data.get("dashboard_chart_chat_id")
        previous_message_id = previous_data.get("dashboard_chart_message_id")
        if isinstance(previous_chat_id, int) and isinstance(previous_message_id, int):
            try:
                await callback.message.bot.delete_message(previous_chat_id, previous_message_id)
            except TelegramAPIError:
                logger.info("Не удалось удалить предыдущую диаграмму дашборда.")
        try:
            chart_message = await callback.message.answer_photo(
                BufferedInputFile(render_dashboard_chart(dashboard), filename="dashboard.png"),
                caption=(
                    f"📈 {dashboard.label}: заявки по дням, статусы, оценки и жалобы.\n"
                    "График жалоб: качество · сроки · стоимость · общение · другое."
                ),
            )
        except (OSError, TelegramAPIError):
            logger.info("Не удалось отправить диаграмму дашборда.")
        else:
            await state.update_data(
                dashboard_chart_chat_id=chart_message.chat.id,
                dashboard_chart_message_id=chart_message.message_id,
            )


show_statistics = show_dashboard


@router.callback_query(F.data.startswith("menu:complaints"))
async def show_complaints(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает администратору жалобы клиентов с постраничной навигацией."""
    if callback.from_user is None:
        await callback.answer("🔒 Этот раздел доступен только администратору.", show_alert=True)
        return
    await state.clear()
    try:
        requested_page = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        requested_page = 0
    async with SessionLocal() as db:
        admin = await get_user_by_telegram_id(db, str(callback.from_user.id))
        complaints = await list_complaints(db) if admin and admin.role == "admin" else None
    if complaints is None:
        await callback.answer("🔒 Этот раздел доступен только администратору.", show_alert=True)
        return
    await callback.answer()
    if callback.message is None:
        return
    if not complaints:
        await edit_callback_screen(
            callback,
            "⚠️ <b>Жалобы</b>\n\n📭 Жалоб пока нет.",
            reply_markup=menu_for_user(callback.from_user.id, "admin"),
            parse_mode="HTML",
        )
        return
    total_pages = ceil(len(complaints) / COMPLAINTS_PER_PAGE)
    page = min(max(requested_page, 0), total_pages - 1)
    start = page * COMPLAINTS_PER_PAGE
    lines = [f"⚠️ <b>Жалобы</b> · {len(complaints)}"]
    for complaint in complaints[start:start + COMPLAINTS_PER_PAGE]:
        lines.append(
            f"\n\n🧾 <b>Заявка №{complaint.request_id}</b> · "
            f"{complaint.created_at:%d.%m.%Y}\n"
            f"Категория: {COMPLAINT_LABELS.get(complaint.category, complaint.category)}\n"
            f"Мастер: {html.escape(complaint.master_name)}\n"
            f"{html.escape(complaint.message) or 'Без комментария'}"
        )
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton(text="← Предыдущие", callback_data=f"menu:complaints:{page - 1}"))
    navigation.append(InlineKeyboardButton(text=f"{page + 1} / {total_pages}", callback_data="menu:complaints:info"))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(text="Следующие →", callback_data=f"menu:complaints:{page + 1}"))
    menu = menu_for_user(callback.from_user.id, "admin")
    await edit_callback_screen(
        callback,
        "".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[navigation, *menu.inline_keyboard]),
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
        expected_role = configured_role_for_telegram_id(callback.from_user.id)
        if user and expected_role and user.role != expected_role:
            # .env задаёт начальные роли; назначения сайта сохраняются в БД.
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
