"""Общие проверки, форматирование и функции экранов."""

import logging
import html
import os
import re
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from repair_bot.database.crud.requests import get_request_status_counts
from repair_bot.database.crud.users import get_all_users
from repair_bot.keyboards import main_menu_keyboard
from repair_bot.database.models import RepairRequest

logger = logging.getLogger("repair_bot.handlers")

REQUESTS_PER_PAGE = 1

MAX_REQUEST_DESCRIPTION_LENGTH = 4000

REQUEST_DESCRIPTION_PREVIEW_LENGTH = 2500

REQUEST_ADDITION_PREFIX = "\n\nДополнение клиента:\n"

CLIENT_EDITABLE_STATUSES = {"new", "in_progress"}

_processed_start_messages: dict[tuple[int, int], float] = {}

_demo_view_roles: dict[int, str] = {}

def is_demo_switch_user(telegram_id: int) -> bool:
    """Демонстрационные панели доступны всем администраторам из доверенного списка."""
    return is_admin_telegram_id(telegram_id)

def menu_for_user(telegram_id: int, actual_role: str):
    """Строит меню, не меняя реальную роль пользователя."""
    demo_role = _demo_view_roles.get(telegram_id) if is_demo_switch_user(telegram_id) else None
    return main_menu_keyboard(
        demo_role or actual_role,
        demo_role=demo_role,
        allow_role_switch=is_demo_switch_user(telegram_id) and actual_role == "admin",
    )

def is_admin_telegram_id(telegram_id: int) -> bool:
    """Сверяет числовой Telegram ID с доверенным списком из .env."""
    configured_ids = {
        value.strip()
        for value in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
        if value.strip().isdigit()
    }
    return str(telegram_id) in configured_ids

def is_master_telegram_id(telegram_id: int) -> bool:
    """Проверяет числовой Telegram ID в списке мастеров из .env."""
    configured_ids = {
        value.strip()
        for value in os.getenv("MASTER_TELEGRAM_IDS", "").split(",")
        if value.strip().isdigit()
    }
    return str(telegram_id) in configured_ids

def role_for_telegram_id(telegram_id: int) -> str:
    """Определяет действительную роль по доверенным спискам ID."""
    if is_admin_telegram_id(telegram_id):
        return "admin"
    if is_master_telegram_id(telegram_id):
        return "master"
    return "client"

def is_staff_telegram_id(telegram_id: int) -> bool:
    """Мастер и администратор могут обрабатывать заявки."""
    return is_admin_telegram_id(telegram_id) or is_master_telegram_id(telegram_id)

def normalize_phone(value: str) -> str | None:
    """Проверяет российский номер и приводит его к виду +7XXXXXXXXXX."""
    value = value.strip()
    if not value or re.search(r"[^\d+()\s.-]", value) or value.count("+") > 1:
        return None
    if "+" in value and not value.startswith("+"):
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        national = digits
    elif len(digits) == 11 and digits[0] in {"7", "8"}:
        national = digits[1:]
    else:
        return None
    return f"+7{national}"

def admin_request_text(request: RepairRequest) -> str:
    """Собирает текст карточки заявки для административного раздела."""
    status_labels = {
        "new": "🆕 Новая",
        "in_progress": "🔧 В работе",
        "completed": "✅ Завершена",
        "cancelled": "❌ Отменена",
    }
    priority_labels = {"low": "🟢 Низкий", "medium": "🟡 Средний", "high": "🔴 Высокий"}
    text = (
        f"🧾 <b>Заявка №{request.id}</b>\n👤 Пользователь: {html.escape(request.user.full_name)}\n"
        f"📱 Телефон: {html.escape(request.user.phone)}\n"
        f"🎯 Приоритет: {priority_labels.get(request.priority, request.priority)}\n"
        f"📌 Статус: {status_labels.get(request.status, request.status)}\n\n"
        f"{html.escape(request_description_preview(request.description))}"
    )
    assessment = getattr(request, "ai_assessment", None)
    if assessment is None:
        return f"{text}\n\n⚠️ ИИ-диагностика недоступна."
    cost = format_rubles(assessment.estimated_cost_kopecks)
    price = format_rubles(assessment.client_price_kopecks)
    urgency = {"low": "Плановая", "medium": "В ближайшее время", "high": "Срочная"}.get(request.priority, request.priority)
    return (
        f"{text}\n\n🤖 <b>Предварительная оценка ИИ</b>\n\n"
        f"<b>⏱ Срочность</b>\n{urgency}\n\n"
        f"<b>🔎 Предварительная причина</b>\n{html.escape(assessment.master_diagnosis)}\n\n"
        f"<b>🧩 Детали для проверки</b>\n{html.escape(assessment.parts)}\n\n"
        f"<b>🛠 Инструменты</b>\n{html.escape(assessment.tools)}\n\n"
        f"<b>💰 Расчёт</b>\nСебестоимость: {cost}\nЦена для клиента: {price}"
    )

def client_request_text(request: RepairRequest) -> str:
    """Формирует компактную карточку заявки для клиента."""
    status_labels = {
        "new": "🆕 Новая",
        "in_progress": "🔧 В работе",
        "completed": "✅ Завершена",
        "cancelled": "❌ Отменена",
    }
    priority_labels = {"low": "🟢 Низкий", "medium": "🟡 Средний", "high": "🔴 Высокий"}
    text = (
        f"🧾 <b>Заявка №{request.id}</b>\n"
        f"📌 Статус: {status_labels.get(request.status, request.status)}\n"
        f"🎯 Приоритет: {priority_labels.get(request.priority, request.priority)}\n\n"
        f"{html.escape(request_description_preview(request.description))}"
    )
    assessment = getattr(request, "ai_assessment", None)
    if assessment is None:
        return text
    price = format_rubles(assessment.client_price_kopecks)
    return (
        f"{text}\n\n🤖 <b>Предварительная оценка ИИ</b>\n\n"
        f"<b>🔎 Возможные причины</b>\n{html.escape(assessment.client_causes)}\n\n"
        f"<b>🛡 Безопасные рекомендации</b>\n{html.escape(assessment.client_recommendations)}\n\n"
        f"⚠️ {html.escape(assessment.safety_note)}\n\n"
        f"<b>💰 Ориентировочная цена</b>\n{price}\n\n"
        "ℹ️ Точный диагноз и цену подтвердит мастер."
    )

def format_rubles(kopecks: int | None) -> str:
    """Форматирует цену или сообщает, что её определит мастер."""
    if kopecks is None:
        return "определит мастер после диагностики"
    return f"{kopecks / 100:,.2f} ₽".replace(",", " ").replace(".00", "")

def request_description_preview(description: str) -> str:
    """Сохраняет последнее уточнение видимым в ограниченной карточке Telegram."""
    if len(description) <= REQUEST_DESCRIPTION_PREVIEW_LENGTH:
        return description
    return f"…{description[-(REQUEST_DESCRIPTION_PREVIEW_LENGTH - 1):]}"

async def edit_callback_screen(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup=None,
    parse_mode: str | None = None,
) -> None:
    """Обновляет экран в сообщении кнопки и спокойно переживает повторный выбор."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise

async def show_start_screen(
    message: Message,
    telegram_id: int,
    text: str,
    *,
    reply_markup=None,
    parse_mode: str | None = None,
) -> tuple[int, int]:
    """Отправляет новое стартовое сообщение для каждого вызова /start."""
    sent = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    return sent.chat.id, sent.message_id

async def safe_edit_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup=None,
    parse_mode: str | None = None,
) -> None:
    """Редактирует экран и игнорирует только ответ Telegram об отсутствии изменений."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise

async def edit_state_screen(
    message: Message,
    state: FSMContext,
    text: str,
    *,
    reply_markup=None,
    parse_mode: str | None = None,
) -> None:
    """Обновляет исходный вопрос FSM, не добавляя отдельный ответ бота."""
    screen = await state.get_data()
    chat_id = screen.get("screen_chat_id")
    message_id = screen.get("screen_message_id")
    if chat_id and message_id:
        await safe_edit_message(
            message.bot,
            chat_id,
            message_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    else:
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)

def bounded_page(page: int, item_count: int, page_size: int = REQUESTS_PER_PAGE) -> tuple[int, int]:
    """Ограничивает номер страницы и возвращает число страниц (минимум одна)."""
    total_pages = max(1, (item_count + page_size - 1) // page_size)
    return min(max(page, 0), total_pages - 1), total_pages

async def dashboard_text(db, user, displayed_role: str) -> str:
    """Возвращает приветственный экран с краткой сводкой соответствующей роли."""
    if displayed_role == "client":
        return (
            f"🖥 <b>ПК Доктор</b>\nЗдравствуйте, {html.escape(user.full_name)}!\n\n"
            "Компьютер не включается, шумит или работает медленно?\n"
            "Оставьте заявку — статус ремонта можно будет проверить здесь же.\n\n"
            "Выберите действие:"
        )
    if displayed_role == "master":
        counts = await get_request_status_counts(db)
        return (
            f"🛠 <b>Панель мастера</b>\nЗдравствуйте, {html.escape(user.full_name)}!\n\n"
            f"📥 Новые заявки: {counts['new']}\n🔧 В работе: {counts['in_progress']}\n\n"
            "Выберите заявку, чтобы посмотреть описание и обновить её статус."
        )
    users = await get_all_users(db)
    return (
        f"⚙️ <b>Панель администратора</b>\nЗдравствуйте, {html.escape(user.full_name)}!\n\n"
        f"👤 Клиентов: {sum(item.role == 'client' for item in users)}\n"
        f"🛠 Мастеров: {sum(item.role == 'master' for item in users)}\n"
        f"🛡 Администраторов: {sum(item.role == 'admin' for item in users)}\n\n"
        "Выберите раздел:"
    )

async def cancel_dialog(message: Message, state: FSMContext) -> None:
    """Сбрасывает активную регистрацию или создание заявки."""
    screen = await state.get_data()
    await state.clear()
    screen_chat_id = screen.get("screen_chat_id")
    screen_message_id = screen.get("screen_message_id")
    if screen_chat_id and screen_message_id:
        await safe_edit_message(
            message.bot,
            screen_chat_id,
            screen_message_id,
            "↩️ Действие отменено. Нажмите /start, чтобы открыть меню.",
        )
    else:
        await message.answer("↩️ Действие отменено. Нажмите /start, чтобы открыть меню.")
