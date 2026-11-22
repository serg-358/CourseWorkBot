"""Обработчики раздела admin."""

import html
from math import ceil

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from repair_bot.database.crud.feedback import list_complaints
from repair_bot.database.crud.requests import get_all_requests
from repair_bot.database.crud.users import get_all_users, get_user_by_telegram_id
from repair_bot.database.models import RepairRating, RequestAssignment, RequestCompletion
from repair_bot.database.session import SessionLocal
from repair_bot.keyboards import main_menu_keyboard
from repair_bot.services.admin_dashboard import (
    build_dashboard_data,
    build_dashboard_xlsx,
    dashboard_detail_message,
    dashboard_message,
    format_completion_duration,
    moscow_day,
    moscow_datetime,
)
from repair_bot.web.workspace import request_price
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
    details = [
        InlineKeyboardButton(text="Динамика", callback_data=f"menu:dashboard:view:dynamics:{period}"),
        InlineKeyboardButton(text="Стоимость ремонтов", callback_data=f"menu:dashboard:view:cost:{period}"),
    ]
    details_second = [
        InlineKeyboardButton(text="Оценки и жалобы", callback_data=f"menu:dashboard:view:feedback:{period}"),
        InlineKeyboardButton(text="Мастера", callback_data=f"menu:dashboard:view:masters:{period}"),
    ]
    actions = [
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"menu:dashboard:{period}"),
        InlineKeyboardButton(
            text="⬇️ Скачать XLSX",
            callback_data=f"menu:dashboard:export:{period}",
        ),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[periods, details, details_second, actions, [InlineKeyboardButton(text="← Главное меню", callback_data="menu:profile")]])


def dashboard_export_extras(dashboard, requests, ratings, complaints, completions, assignments) -> dict[str, object]:
    """Готовит для XLSX те же финансовые строки, что видит администратор."""
    completed = [item for item in requests if item.status == "completed" and dashboard.period_start <= moscow_day(completions.get(item.id, item.created_at)) <= dashboard.period_end]
    cost = sum(request_price(item) or 0 for item in completed)
    durations = [max(0, (completions[item.id] - item.created_at).total_seconds()) for item in completed if item.id in completions]
    ratings_by_request = {row.request_id: row for row in ratings}
    clients: dict[str, int] = {}
    masters: dict[str, list] = {}
    for item in completed:
        amount = request_price(item) or 0
        clients[item.user.full_name] = clients.get(item.user.full_name, 0) + amount
        name = assignments[item.id].master_name if item.id in assignments else "Не назначен"
        stats = masters.setdefault(name, [0, 0, [], 0])
        stats[0], stats[1] = stats[0] + 1, stats[1] + amount
        if item.id in ratings_by_request:
            stats[2].append(ratings_by_request[item.id].score)
        stats[3] += sum(row.request_id == item.id for row in complaints if dashboard.period_start <= moscow_day(row.created_at) <= dashboard.period_end)
    return {
        "completed_cost": cost,
        "average_check": cost // len(completed) if completed else 0,
        "cancellation_rate": dashboard.status_counts["cancelled"] * 100 / max(dashboard.request_count, 1),
        "feedback_rate": sum(dashboard.period_start <= moscow_day(row.created_at) <= dashboard.period_end for row in ratings) * 100 / max(len(completed), 1),
        "average_completion_duration": format_completion_duration(sum(durations) / len(durations) if durations else None),
        "top_clients": sorted(clients.items(), key=lambda row: row[1], reverse=True)[:5],
        "masters": [(name, values[0], values[1], sum(values[2]) / len(values[2]) if values[2] else None, values[3]) for name, values in sorted(masters.items(), key=lambda row: row[1][1], reverse=True)],
    }

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

@router.callback_query(F.data.startswith("menu:dashboard:export:"))
async def export_dashboard(callback: CallbackQuery) -> None:
    """Отправляет администратору XLSX-выгрузку выбранного периода."""
    if callback.from_user is None:
        return
    period = (callback.data or "").rsplit(":", 1)[-1]
    period = period if period in DASHBOARD_PERIODS else "7"
    async with SessionLocal() as db:
        admin = await get_user_by_telegram_id(db, str(callback.from_user.id))
        if admin is None or admin.role != "admin":
            await callback.answer("🔒 Этот раздел доступен только администратору.", show_alert=True)
            return
        requests = await get_all_requests(db) + await get_all_requests(db, trash=True)
        complaints = await list_complaints(db)
        ratings = list((await db.scalars(select(RepairRating))).all())
        completions = {row.request_id: row.completed_at for row in (await db.scalars(select(RequestCompletion))).all()}
        assignments = {row.request_id: row for row in (await db.scalars(select(RequestAssignment))).all()}
    await callback.answer("Готовлю XLSX…")
    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(
                build_dashboard_xlsx(
                    build_dashboard_data(requests, ratings, complaints, period),
                    extras=dashboard_export_extras(build_dashboard_data(requests, ratings, complaints, period), requests, ratings, complaints, completions, assignments),
                ),
                filename=f"dashboard-{period}.xlsx",
            ),
            caption=f"Статистика дашборда · {period}.",
        )


@router.callback_query(F.data.startswith("menu:dashboard"))
@router.callback_query(F.data == "menu:statistics")
async def show_dashboard(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает администратору сводку по пользователям, заявкам и отзывам."""
    if callback.from_user is None:
        await callback.answer("🔒 Этот раздел доступен только администратору.", show_alert=True)
        return
    await state.clear()
    parts = (callback.data or "").split(":")
    period = parts[-1]
    period = period if period in DASHBOARD_PERIODS else "7"
    section = parts[-2] if len(parts) >= 5 and parts[-3] == "view" else "summary"

    async with SessionLocal() as db:
        admin = await get_user_by_telegram_id(db, str(callback.from_user.id))
        if admin is None or admin.role != "admin":
            users = None
        else:
            users = await get_all_users(db)
            requests = await get_all_requests(db) + await get_all_requests(db, trash=True)
            complaints = await list_complaints(db)
            ratings = list((await db.scalars(select(RepairRating))).all())
            completions = {
                row.request_id: row.completed_at
                for row in (await db.scalars(select(RequestCompletion))).all()
            }
            assignments = {
                row.request_id: row
                for row in (await db.scalars(select(RequestAssignment))).all()
            }

    if users is None:
        await callback.answer("🔒 Этот раздел доступен только администратору.", show_alert=True)
        return

    dashboard = build_dashboard_data(requests, ratings, complaints, period)
    completed = [
        item
        for item in requests
        if item.status == "completed"
        and dashboard.period_start <= moscow_day(completions.get(item.id, item.created_at)) <= dashboard.period_end
    ]
    revenue = sum(request_price(item) or 0 for item in completed)
    completion_durations = [
        max(0, (completions[item.id] - item.created_at).total_seconds())
        for item in completed
        if item.id in completions
    ]
    ratings_by_request = {rating.request_id: rating for rating in ratings}
    completed_ratings = [ratings_by_request[item.id] for item in completed if item.id in ratings_by_request]
    period_complaints = [
        complaint
        for complaint in complaints
        if dashboard.period_start <= moscow_day(complaint.created_at) <= dashboard.period_end
    ]
    client_revenue: dict[str, int] = {}
    master_stats: dict[str, list] = {}
    for item in completed:
        amount = request_price(item) or 0
        client_revenue[item.user.full_name] = client_revenue.get(item.user.full_name, 0) + amount
        assignment = assignments.get(item.id)
        name = assignment.master_name if assignment else "Не назначен"
        stats = master_stats.setdefault(name, [0, 0, [], 0])
        stats[0] += 1
        stats[1] += amount
        if item.id in ratings_by_request:
            stats[2].append(ratings_by_request[item.id].score)
        stats[3] += sum(complaint.request_id == item.id for complaint in period_complaints)
    top_clients = sorted(client_revenue.items(), key=lambda row: row[1], reverse=True)[:3]
    master_rows = [
        (name, stats[0], stats[1], sum(stats[2]) / len(stats[2]) if stats[2] else None, stats[3])
        for name, stats in sorted(master_stats.items(), key=lambda row: row[1][1], reverse=True)[:5]
    ]
    await callback.answer()
    if callback.message:
        await edit_callback_screen(
            callback,
            dashboard_message(dashboard) if section == "summary" else dashboard_detail_message(
                section,
                dashboard,
                revenue_kopecks=revenue,
                average_check_kopecks=revenue // len(completed) if completed else 0,
                cancellation_rate=dashboard.status_counts["cancelled"] * 100 / max(dashboard.request_count, 1),
                feedback_rate=len(completed_ratings) * 100 / max(len(completed), 1),
                average_completion_duration=(sum(completion_durations) / len(completion_durations) if completion_durations else None),
                top_clients=top_clients,
                master_rows=master_rows,
            ),
            reply_markup=dashboard_keyboard(callback.from_user.id, period),
            parse_mode="HTML",
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
            f"{moscow_datetime(complaint.created_at):%d.%m.%Y}\n"
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
