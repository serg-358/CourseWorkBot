"""Обработчики запуска и регистрации пользователя."""

import html
import logging
import os
import re
import time
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from crud.requests import (
    create_repair_request,
    get_all_requests,
    get_repair_request,
    get_request_status_counts,
    get_user_requests,
    save_ai_assessment,
    update_request_status,
)
from crud.users import create_user, get_all_users, get_user_by_telegram_id
from database import SessionLocal
from keyboards import (
    dialog_keyboard,
    main_menu_keyboard,
    photo_dialog_keyboard,
    request_navigation_keyboard,
)
from models import RepairRequest
from states import RegistrationStates, RequestStates
from ai_service import analyze_repair_request
from pricing_catalog import calculate_cost

router = Router(name="auth")
logger = logging.getLogger(__name__)
REQUESTS_PER_PAGE = 1

# Защищаем от повторной обработки одного и того же Telegram-сообщения /start.
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
        "new": "Новая",
        "in_progress": "В работе",
        "completed": "Завершена",
        "cancelled": "Отменена",
    }
    priority_labels = {"low": "Низкий", "medium": "Средний", "high": "Высокий"}
    text = (
        f"Заявка №{request.id}\nПользователь: {html.escape(request.user.full_name)}\n"
        f"Телефон: {html.escape(request.user.phone)}\n"
        f"Приоритет: {priority_labels.get(request.priority, request.priority)}\n"
        f"Статус: {status_labels.get(request.status, request.status)}\n\n"
        f"{html.escape(request.description[:2500])}"
    )
    assessment = getattr(request, "ai_assessment", None)
    if assessment is None:
        return f"{text}\n\nИИ-диагностика недоступна."
    cost = format_rubles(assessment.estimated_cost_kopecks)
    price = format_rubles(assessment.client_price_kopecks)
    urgency = {"low": "Плановая", "medium": "В ближайшее время", "high": "Срочная"}.get(request.priority, request.priority)
    return (
        f"{text}\n\n<b>Предварительная оценка ИИ</b>\nСрочность: {urgency}\n"
        f"Причина: {html.escape(assessment.master_diagnosis)}\n"
        f"Детали: {html.escape(assessment.parts)}\nИнструменты: {html.escape(assessment.tools)}\n"
        f"Себестоимость: {cost}\nЦена для клиента: {price}"
    )


def client_request_text(request: RepairRequest) -> str:
    """Формирует компактную карточку заявки для клиента."""
    status_labels = {
        "new": "Новая",
        "in_progress": "В работе",
        "completed": "Завершена",
        "cancelled": "Отменена",
    }
    priority_labels = {"low": "Низкий", "medium": "Средний", "high": "Высокий"}
    text = (
        f"🧾 <b>Заявка №{request.id}</b>\n"
        f"Статус: {status_labels.get(request.status, request.status)}\n"
        f"Приоритет: {priority_labels.get(request.priority, request.priority)}\n\n"
        f"{html.escape(request.description[:2500])}"
    )
    assessment = getattr(request, "ai_assessment", None)
    if assessment is None:
        return text
    price = format_rubles(assessment.client_price_kopecks)
    return (
        f"{text}\n\n<b>Предварительная оценка ИИ</b>\n"
        f"Возможные причины: {html.escape(assessment.client_causes)}\n"
        f"Рекомендации: {html.escape(assessment.client_recommendations)}\n"
        f"{html.escape(assessment.safety_note)}\n"
        f"Ориентировочная цена: {price}\n"
        "Точный диагноз и цену подтвердит мастер."
    )


def format_rubles(kopecks: int | None) -> str:
    """Форматирует цену или сообщает, что её определит мастер."""
    if kopecks is None:
        return "определит мастер после диагностики"
    return f"{kopecks / 100:,.2f} ₽".replace(",", " ").replace(".00", "")


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
            "Действие отменено. Нажмите /start, чтобы открыть меню.",
        )
    else:
        await message.answer("Действие отменено. Нажмите /start, чтобы открыть меню.")


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    await cancel_dialog(message, state)


@router.callback_query(F.data == "dialog:cancel")
async def cancel_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Действие отменено.")
    if callback.message:
        role = "client"
        if callback.from_user:
            async with SessionLocal() as db:
                user = await get_user_by_telegram_id(db, str(callback.from_user.id))
                if user:
                    role = user.role
        await edit_callback_screen(
            callback,
            "Действие отменено. Нажмите /start, чтобы открыть меню.",
            reply_markup=(
                menu_for_user(callback.from_user.id, role)
                if callback.from_user and role != "client"
                else main_menu_keyboard("client")
            ),
        )


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Проверяет регистрацию и начинает диалог с пользователем."""
    if message.from_user is None:
        return

    # Повторно доставленный update не должен создавать ещё одно приветствие и меню.
    now = time.monotonic()
    message_key = (message.chat.id, message.message_id)
    if message_key in _processed_start_messages:
        return
    _processed_start_messages[message_key] = now
    for key, processed_at in list(_processed_start_messages.items()):
        if now - processed_at > 300:
            del _processed_start_messages[key]

    await state.clear()

    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(message.from_user.id))
        status_counts = None
        admin_counts = None
        if user:
            expected_role = role_for_telegram_id(message.from_user.id)
        else:
            expected_role = None
        if user and user.role != expected_role:
            # Роль определяется только числовым Telegram ID из списка доверия.
            user.role = expected_role
            await db.commit()
        if user and user.role in {"admin", "master"}:
            status_counts = await get_request_status_counts(db)
        if user and user.role == "admin":
            all_users = await get_all_users(db)
            admin_counts = {
                "clients": sum(item.role == "client" for item in all_users),
                "masters": sum(item.role == "master" for item in all_users),
                "admins": sum(item.role == "admin" for item in all_users),
            }

    if user:
        displayed_role = (
            _demo_view_roles.get(message.from_user.id, user.role)
            if is_demo_switch_user(message.from_user.id)
            else user.role
        )
        if displayed_role == "client":
            welcome_text = (
                f"🖥 <b>ПК Доктор</b>\nЗдравствуйте, {html.escape(user.full_name)}!\n\n"
                "Компьютер не включается, шумит или работает медленно?\n"
                "Оставьте заявку — опишите, что случилось, а статус ремонта "
                "можно будет проверить здесь же.\n\n"
                "Выберите действие:"
            )
        elif displayed_role == "master":
            welcome_text = (
                f"🛠 <b>Панель мастера</b>\nЗдравствуйте, {html.escape(user.full_name)}!\n\n"
                f"📥 Новые заявки: {status_counts['new'] if status_counts else 0}\n"
                f"🔧 В работе: {status_counts['in_progress'] if status_counts else 0}\n\n"
                "Выберите заявку, чтобы посмотреть описание и обновить её статус."
            )
        else:
            welcome_text = (
                f"⚙️ <b>Панель администратора</b>\nЗдравствуйте, {html.escape(user.full_name)}!\n\n"
                f"👤 Клиентов: {admin_counts['clients'] if admin_counts else 0}\n"
                f"🛠 Мастеров: {admin_counts['masters'] if admin_counts else 0}\n\n"
                f"🛡 Администраторов: {admin_counts['admins'] if admin_counts else 0}\n\n"
                "Выберите раздел:"
            )
        await show_start_screen(
            message,
            message.from_user.id,
            welcome_text,
            reply_markup=menu_for_user(message.from_user.id, user.role),
            parse_mode="HTML",
        )
        return

    screen_chat_id, screen_message_id = await show_start_screen(
        message,
        message.from_user.id,
        "🖥 <b>ПК Доктор</b>\nПомогаем оформить заявку на ремонт компьютера.\n\n"
        "Опишите неисправность, а статус заявки сможете проверять в боте.\n\n"
        "Для начала создадим профиль. Как вас зовут?",
        reply_markup=dialog_keyboard(main_menu_keyboard("client")),
        parse_mode="HTML",
    )
    await state.set_state(RegistrationStates.waiting_full_name)
    await state.update_data(
        screen_chat_id=screen_chat_id,
        screen_message_id=screen_message_id,
    )


@router.callback_query(F.data.in_({"view:admin", "view:master", "view:client", "demo:toggle"}))
async def switch_demo_view(callback: CallbackQuery, state: FSMContext) -> None:
    """Переключает демонстрационные панели только для владельца-администратора."""
    if callback.from_user is None or not is_demo_switch_user(callback.from_user.id):
        await callback.answer("Переключение доступно только владельцу бота.", show_alert=True)
        return
    await state.clear()

    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        if user is None or user.role != "admin":
            await callback.answer("Для переключения нужна роль администратора.", show_alert=True)
            return

    requested_view = "client" if callback.data == "demo:toggle" else (callback.data or "").split(":", 1)[-1]
    if requested_view == "admin":
        _demo_view_roles.pop(callback.from_user.id, None)
        role = "admin"
    else:
        _demo_view_roles[callback.from_user.id] = requested_view
        role = requested_view

    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        text = await dashboard_text(db, user, role) if user else "Профиль не найден. Нажмите /start."
    await callback.answer("Экран обновлён.")
    await edit_callback_screen(
        callback,
        text,
        reply_markup=main_menu_keyboard(
            role,
            demo_role=None if role == "admin" else role,
            allow_role_switch=True,
        ),
        parse_mode="HTML",
    )


@router.message(RegistrationStates.waiting_full_name, F.text)
async def process_full_name(message: Message, state: FSMContext) -> None:
    """Проверяет имя, сохраняет его в FSM и запрашивает телефон."""
    full_name = (message.text or "").strip()
    if len(full_name) < 3:
        await edit_state_screen(
            message,
            state,
            "Имя должно содержать минимум 3 символа. Введите имя ещё раз:",
            reply_markup=dialog_keyboard(main_menu_keyboard("client")),
        )
        return

    await state.update_data(full_name=full_name)
    await edit_state_screen(
        message,
        state,
        "Ваш телефон? Например: 89999999999 или +7 (999) 999-99-99",
        reply_markup=dialog_keyboard(main_menu_keyboard("client")),
    )
    await state.set_state(RegistrationStates.waiting_phone)


@router.message(RegistrationStates.waiting_full_name)
async def process_non_text_name(message: Message, state: FSMContext) -> None:
    """Просит отправить имя текстовым сообщением."""
    await edit_state_screen(
        message,
        state,
        "Пожалуйста, отправьте имя текстом.",
        reply_markup=dialog_keyboard(main_menu_keyboard("client")),
    )


@router.message(RegistrationStates.waiting_phone, F.text)
async def process_phone(message: Message, state: FSMContext) -> None:
    """Проверяет телефон, создаёт пользователя и показывает меню."""
    phone = normalize_phone(message.text or "")
    if phone is None:
        await edit_state_screen(
            message,
            state,
            "Введите российский номер: 89999999999 или +7 (999) 999-99-99.",
            reply_markup=dialog_keyboard(main_menu_keyboard("client")),
        )
        return
    if message.from_user is None:
        return

    data = await state.get_data()
    full_name = data.get("full_name")
    if not full_name:
        await state.clear()
        await message.answer("Не удалось продолжить регистрацию. Нажмите /start.")
        return

    async with SessionLocal() as db:
        # Если две регистрации начались одновременно, повторная проверка не даст создать дубль.
        user = await get_user_by_telegram_id(db, str(message.from_user.id))
        if user is None:
            user = await create_user(
                db,
                telegram_id=str(message.from_user.id),
                full_name=full_name,
                phone=phone,
                role=role_for_telegram_id(message.from_user.id),
            )

    registration_messages = {
        "client": (
            "Регистрация успешна ✅\n\n"
            "Теперь вы можете создать заявку и отслеживать её статус в разделе «Мои заявки»."
        ),
        "master": "Регистрация успешна ✅ Вы вошли в панель мастера.",
        "admin": "Регистрация успешна ✅ Вы вошли в панель администратора.",
    }
    if data.get("screen_message_id"):
        await safe_edit_message(
            message.bot,
            data["screen_chat_id"],
            data["screen_message_id"],
            registration_messages.get(user.role, "Регистрация успешна ✅"),
            reply_markup=menu_for_user(message.from_user.id, user.role),
        )
    else:
        await message.answer(
            registration_messages.get(user.role, "Регистрация успешна ✅"),
            reply_markup=menu_for_user(message.from_user.id, user.role),
        )
    await state.clear()


@router.message(RegistrationStates.waiting_phone)
async def process_non_text_phone(message: Message, state: FSMContext) -> None:
    """Просит отправить телефон текстовым сообщением."""
    await edit_state_screen(
        message,
        state,
        "Пожалуйста, отправьте номер телефона текстом.",
        reply_markup=dialog_keyboard(main_menu_keyboard("client")),
    )


@router.callback_query(F.data == "menu:requests")
@router.callback_query(F.data.startswith("requests:client:"))
async def show_my_requests(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает по одной заявке клиента в редактируемом экране."""
    if callback.from_user is None:
        return
    await state.clear()
    try:
        requested_page = int((callback.data or "").rsplit(":", 1)[-1]) if callback.data != "menu:requests" else 0
    except ValueError:
        requested_page = 0
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        if user:
            requests = await get_user_requests(db, user.id)
            role = user.role
        else:
            requests, role = [], "client"

    page, total_pages = bounded_page(requested_page, len(requests))
    await callback.answer()
    if not user:
        text = "Сначала зарегистрируйтесь командой /start."
        markup = main_menu_keyboard("client")
    elif not requests:
        text = "📭 У вас пока нет заявок.\n\nСоздайте первую — мастер свяжется с вами после проверки."
        markup = menu_for_user(callback.from_user.id, role)
    else:
        request = requests[page]
        text = client_request_text(request)
        markup = request_navigation_keyboard(
            view="client",
            page=page,
            total_pages=total_pages,
            menu=menu_for_user(callback.from_user.id, role),
        )
    await edit_callback_screen(callback, text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("requests:page-info"))
async def show_page_info(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
        await callback.answer(f"Страница {parts[2]} из {parts[3]}")
    else:
        await callback.answer()


@router.callback_query(F.data == "menu:create")
async def start_request_creation(callback: CallbackQuery, state: FSMContext) -> None:
    """Начинает ввод описания новой заявки."""
    if callback.message is None or callback.from_user is None:
        return
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
    if not user:
        await callback.answer("Сначала зарегистрируйтесь командой /start.", show_alert=True)
        return
    role = user.role
    await state.clear()
    await state.set_state(RequestStates.waiting_description)
    await callback.answer()
    await callback.message.answer(
        "Опишите неисправность компьютера. Мастер увидит это описание в заявке.",
        reply_markup=dialog_keyboard(menu_for_user(callback.from_user.id, role)),
    )


@router.message(RequestStates.waiting_description, F.text)
async def process_request_description(message: Message, state: FSMContext) -> None:
    """Принимает описание и переходит к необязательному фото ошибки."""
    if message.from_user is None:
        return
    description = (message.text or "").strip()
    if len(description) < 10:
        menu = menu_for_user(message.from_user.id, role_for_telegram_id(message.from_user.id))
        await message.answer(
            "Опишите проблему подробнее (не менее 10 символов). Попробуйте ещё раз:",
            reply_markup=dialog_keyboard(menu),
        )
        return
    await state.update_data(description=description[:4000])
    await state.set_state(RequestStates.waiting_photo)
    role = role_for_telegram_id(message.from_user.id)
    await message.answer(
        "Для предварительной AI-оценки описание и необязательное фото ошибки "
        "будут переданы в OpenAI. Пришлите одно фото до 10 МБ или нажмите «Пропустить фото».",
        reply_markup=photo_dialog_keyboard(menu_for_user(message.from_user.id, role)),
    )


@router.message(RequestStates.waiting_description)
async def process_non_text_description(message: Message, state: FSMContext) -> None:
    role = role_for_telegram_id(message.from_user.id) if message.from_user else "client"
    menu = menu_for_user(message.from_user.id, role) if message.from_user else main_menu_keyboard()
    await message.answer(
        "Отправьте описание неисправности текстом или нажмите «Отмена».",
        reply_markup=dialog_keyboard(menu),
    )


async def create_and_analyze_request(
    telegram_id: int,
    description: str,
    photo_file_id: str | None = None,
    photo_bytes: bytes | None = None,
) -> tuple[RepairRequest | None, str]:
    """Надёжно создаёт заявку, затем пытается дополнить её AI-оценкой."""
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(telegram_id))
        if user is None:
            return None, "client"
        request = await create_repair_request(db, user.id, description)
        role = user.role
    try:
        result = await analyze_repair_request(description, photo_bytes)
        if result is not None:
            items, cost, price = calculate_cost(result.catalog_item_codes)
            codes = [item.code for item in items] if cost is not None else []
            async with SessionLocal() as db:
                await save_ai_assessment(
                    db,
                    request.id,
                    priority=result.priority,
                    photo_file_id=photo_file_id,
                    client_causes=result.client_causes,
                    client_recommendations=result.client_recommendations,
                    safety_note=result.safety_note,
                    master_diagnosis=result.master_diagnosis,
                    parts=result.parts,
                    tools=result.tools,
                    catalog_item_codes=",".join(codes),
                    estimated_cost_kopecks=cost,
                    client_price_kopecks=price,
                )
    except Exception:
        logger.exception("ИИ-диагностика заявки №%s недоступна", request.id)
    async with SessionLocal() as db:
        saved_request = await get_repair_request(db, request.id)
    return saved_request, role


async def finish_request_creation(
    message: Message,
    state: FSMContext,
    *,
    photo_file_id: str | None = None,
    photo_bytes: bytes | None = None,
    telegram_id: int | None = None,
) -> None:
    """Завершает FSM и показывает клиенту сохранённую заявку с результатом AI."""
    actor_id = telegram_id or (message.from_user.id if message.from_user else None)
    if actor_id is None:
        return
    data = await state.get_data()
    description = data.get("description")
    if not isinstance(description, str):
        await state.clear()
        await message.answer("Начните создание заявки заново.")
        return
    request, role = await create_and_analyze_request(
        actor_id, description, photo_file_id, photo_bytes
    )
    await state.clear()
    if request is None:
        await message.answer("Сначала зарегистрируйтесь командой /start.")
        return
    text = f"✅ Заявка №{request.id} создана.\n\n{client_request_text(request)}"
    if getattr(request, "ai_assessment", None) is None:
        text += (
            "\n\n⚠️ Предварительная AI-оценка сейчас недоступна. "
            "Заявка передана мастеру, который уточнит причину и стоимость."
        )
    if request.ai_assessment is None:
        text += "\n\nПредварительная AI-оценка сейчас недоступна; заявку рассмотрит мастер."
    await message.answer(text, reply_markup=menu_for_user(actor_id, role), parse_mode="HTML")


@router.callback_query(RequestStates.waiting_photo, F.data == "ai:skip-photo")
async def skip_request_photo(callback: CallbackQuery, state: FSMContext) -> None:
    """Создаёт заявку без изображения."""
    if callback.from_user is None or callback.message is None:
        return
    await callback.answer()
    await finish_request_creation(callback.message, state, telegram_id=callback.from_user.id)


@router.message(RequestStates.waiting_photo, F.photo)
async def process_request_photo(message: Message, state: FSMContext) -> None:
    """Принимает одно фото ошибки и передаёт его AI вместе с описанием."""
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > 10 * 1024 * 1024:
        await message.answer("Фото больше 10 МБ. Пришлите меньшее фото или пропустите этот шаг.")
        return
    try:
        telegram_file = await message.bot.get_file(photo.file_id)
        downloaded = await message.bot.download_file(telegram_file.file_path)
        photo_bytes = downloaded.getvalue() if isinstance(downloaded, BytesIO) else None
    except TelegramAPIError:
        await message.answer("Не удалось получить фото. Попробуйте ещё раз или пропустите этот шаг.")
        return
    await finish_request_creation(
        message, state, photo_file_id=photo.file_id, photo_bytes=photo_bytes
    )


@router.message(RequestStates.waiting_photo)
async def process_invalid_request_photo(message: Message) -> None:
    await message.answer("Пришлите обычное фото ошибки на экране или нажмите «Пропустить фото».")


@router.callback_query(F.data == "menu:all_requests")
@router.callback_query(F.data.startswith("requests:staff:"))
async def show_all_requests(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает сотруднику одну заявку на экране с листанием."""
    if callback.from_user is None or not is_staff_telegram_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только мастеру или администратору.", show_alert=True)
        return
    await state.clear()

    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        role = user.role if user and user.role in {"admin", "master"} else None
        if role is None:
            await callback.answer("Нет прав для просмотра заявок.", show_alert=True)
            return
        requests = await get_all_requests(db)

    try:
        requested_page = int((callback.data or "").rsplit(":", 1)[-1]) if callback.data != "menu:all_requests" else 0
    except ValueError:
        requested_page = 0
    page, total_pages = bounded_page(requested_page, len(requests))

    await callback.answer()
    if callback.message is None:
        return
    if not requests:
        await edit_callback_screen(
            callback,
            "📭 Заявок пока нет.",
            reply_markup=menu_for_user(callback.from_user.id, role),
        )
        return
    request = requests[page]
    await edit_callback_screen(
        callback,
        f"🧾 <b>Заявка {page + 1} из {len(requests)}</b>\n\n{admin_request_text(request)}",
        reply_markup=request_navigation_keyboard(
            view="staff",
            page=page,
            total_pages=total_pages,
            menu=menu_for_user(callback.from_user.id, role),
            request_id=request.id,
            current_status=request.status,
            has_photo=bool(
                getattr(request, "ai_assessment", None)
                and request.ai_assessment.photo_file_id
            ),
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("request:photo:"))
async def show_request_photo(callback: CallbackQuery) -> None:
    """Отправляет исходное фото только сотруднику из доверенного списка."""
    if callback.from_user is None or not is_staff_telegram_id(callback.from_user.id):
        await callback.answer("Это действие доступно только мастеру или администратору.", show_alert=True)
        return
    match = re.fullmatch(r"request:photo:(\d+)", callback.data or "")
    if match is None or callback.message is None:
        await callback.answer("Некорректное фото заявки.", show_alert=True)
        return
    async with SessionLocal() as db:
        request = await get_repair_request(db, int(match.group(1)))
    if request is None or request.ai_assessment is None or not request.ai_assessment.photo_file_id:
        await callback.answer("Фото для этой заявки не найдено.", show_alert=True)
        return
    await callback.message.answer_photo(request.ai_assessment.photo_file_id, caption=f"Фото заявки №{request.id}")
    await callback.answer()


@router.callback_query(F.data.startswith("request:status:"))
async def change_request_status(callback: CallbackQuery, bot: Bot) -> None:
    """Разрешает мастеру и администратору обновлять статус заявки."""
    if callback.from_user is None or not is_staff_telegram_id(callback.from_user.id):
        await callback.answer("Это действие доступно только мастеру или администратору.", show_alert=True)
        return
    match = re.fullmatch(r"request:status:(\d+):(in_progress|completed|cancelled)", callback.data or "")
    if not match:
        await callback.answer("Некорректный статус заявки.", show_alert=True)
        return
    request_id, status = int(match.group(1)), match.group(2)
    async with SessionLocal() as db:
        current_request = await get_repair_request(db, request_id)
        if current_request is None:
            request = None
            previous_status = None
        else:
            previous_status = current_request.status
            await update_request_status(db, request_id, status)
            request = await get_repair_request(db, request_id)
    if request is None:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    changed = previous_status != status
    await callback.answer(
        "Статус обновлён." if changed else "Заявка уже имеет этот статус."
    )
    if callback.message:
        async with SessionLocal() as db:
            requests = await get_all_requests(db)
            role_user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        page = next((index for index, item in enumerate(requests) if item.id == request.id), 0)
        total_pages = max(1, len(requests))
        role = role_user.role if role_user and role_user.role in {"admin", "master"} else "master"
        await edit_callback_screen(
            callback,
            f"🧾 <b>Заявка {page + 1} из {len(requests)}</b>\n\n{admin_request_text(request)}",
            reply_markup=request_navigation_keyboard(
                view="staff",
                page=page,
                total_pages=total_pages,
                menu=menu_for_user(callback.from_user.id, role),
                request_id=request.id,
                current_status=request.status,
                has_photo=bool(
                    getattr(request, "ai_assessment", None)
                    and request.ai_assessment.photo_file_id
                ),
            ),
            parse_mode="HTML",
        )
    if not changed:
        return
    status_labels = {
        "in_progress": "Ваша заявка принята в работу.",
        "completed": "Ваша заявка завершена.",
        "cancelled": "Ваша заявка отменена.",
    }
    try:
        await bot.send_message(request.user.telegram_id, status_labels[status])
    except TelegramAPIError:
        # Пользователь мог заблокировать бота; смена статуса заявки уже сохранена.
        logger.info("Уведомление о заявке №%s не доставлено.", request.id)


@router.callback_query(F.data.startswith("menu:users"))
async def show_users(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает администратору пользователей выбранной роли."""
    if callback.from_user is None or not is_admin_telegram_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только администратору.", show_alert=True)
        return
    await state.clear()

    async with SessionLocal() as db:
        admin = await get_user_by_telegram_id(db, str(callback.from_user.id))
        if admin is None or admin.role != "admin":
            users = None
        else:
            users = await get_all_users(db)

    if users is None:
        await callback.answer("Этот раздел доступен только администратору.", show_alert=True)
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
        empty_labels = {"client": "Клиентов пока нет.", "master": "Мастеров пока нет."}
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
        await callback.answer("Этот раздел доступен только администратору.", show_alert=True)
        return
    await state.clear()

    async with SessionLocal() as db:
        current_user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        users = await get_all_users(db) if current_user and current_user.role == "admin" else None

    if users is None:
        await callback.answer("Нет прав для просмотра раздела.", show_alert=True)
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
        await callback.answer("Этот раздел доступен только администратору.", show_alert=True)
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
        await callback.answer("Этот раздел доступен только администратору.", show_alert=True)
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
                "Профиль не найден. Нажмите /start для регистрации.",
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
