"""Обработчики запуска и регистрации пользователя."""

import html
import logging
import os
import re
import time

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
    update_request_status,
)
from crud.users import create_user, get_all_users, get_user_by_telegram_id
from database import SessionLocal
from keyboards import (
    dialog_keyboard,
    main_menu_keyboard,
    request_navigation_keyboard,
)
from models import RepairRequest
from states import RegistrationStates, RequestStates

router = Router(name="auth")
logger = logging.getLogger(__name__)
REQUESTS_PER_PAGE = 1

# Защищаем от повторной обработки одного и того же Telegram-сообщения /start.
_processed_start_messages: dict[tuple[int, int], float] = {}
_demo_view_roles: dict[int, str] = {}
_last_screen_by_user: dict[int, tuple[int, int]] = {}


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
    return (
        f"Заявка №{request.id}\nПользователь: {html.escape(request.user.full_name)}\n"
        f"Телефон: {html.escape(request.user.phone)}\n"
        f"Приоритет: {priority_labels.get(request.priority, request.priority)}\n"
        f"Статус: {status_labels.get(request.status, request.status)}\n\n"
        f"{html.escape(request.description[:2500])}"
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
    return (
        f"🧾 <b>Заявка №{request.id}</b>\n"
        f"Статус: {status_labels.get(request.status, request.status)}\n"
        f"Приоритет: {priority_labels.get(request.priority, request.priority)}\n\n"
        f"{html.escape(request.description[:2500])}"
    )


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
    if callback.from_user:
        _last_screen_by_user[callback.from_user.id] = (
            callback.message.chat.id,
            callback.message.message_id,
        )


async def show_start_screen(
    message: Message,
    telegram_id: int,
    text: str,
    *,
    reply_markup=None,
    parse_mode: str | None = None,
) -> tuple[int, int]:
    """При повторном /start переиспользует последнее экранное сообщение пользователя."""
    previous_screen = _last_screen_by_user.get(telegram_id)
    if previous_screen and previous_screen[0] == message.chat.id:
        try:
            await safe_edit_message(
                message.bot,
                previous_screen[0],
                previous_screen[1],
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return previous_screen
        except TelegramBadRequest as error:
            description = str(error).lower()
            if not any(
                phrase in description
                for phrase in ("message to edit not found", "message can't be edited", "message can not be edited")
            ):
                raise

    sent = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    screen = (sent.chat.id, sent.message_id)
    _last_screen_by_user[telegram_id] = screen
    return screen


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
    await state.clear()
    await state.set_state(RequestStates.waiting_description)
    await state.update_data(
        screen_chat_id=callback.message.chat.id,
        screen_message_id=callback.message.message_id,
    )
    await callback.answer()
    await edit_callback_screen(
        callback,
        "Опишите неисправность компьютера. Мастер увидит это описание в заявке.",
        reply_markup=dialog_keyboard(menu_for_user(callback.from_user.id, role)),
    )


@router.message(RequestStates.waiting_description, F.text)
async def process_request_description(message: Message, state: FSMContext) -> None:
    """Сохраняет заявку и связывает её с авторизованным пользователем."""
    if message.from_user is None:
        return
    description = (message.text or "").strip()
    if len(description) < 10:
        menu = menu_for_user(message.from_user.id, role_for_telegram_id(message.from_user.id))
        await edit_state_screen(
            message,
            state,
            "Опишите проблему подробнее (не менее 10 символов). Попробуйте ещё раз:",
            reply_markup=dialog_keyboard(menu),
        )
        return
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(message.from_user.id))
        if user is None:
            await state.clear()
            await message.answer("Сначала зарегистрируйтесь командой /start.")
            return
        request = await create_repair_request(db, user.id, description[:4000])
        role = user.role
    screen = await state.get_data()
    await state.clear()
    screen_chat_id = screen.get("screen_chat_id")
    screen_message_id = screen.get("screen_message_id")
    if screen_chat_id and screen_message_id:
        await safe_edit_message(
            message.bot,
            screen_chat_id,
            screen_message_id,
            f"✅ Заявка №{request.id} создана. Текущий статус: новая.",
            reply_markup=menu_for_user(message.from_user.id, role),
        )
    else:
        await message.answer(
        f"Заявка №{request.id} создана. Текущий статус: {request.status}.",
        reply_markup=menu_for_user(message.from_user.id, role),
        )


@router.message(RequestStates.waiting_description)
async def process_non_text_description(message: Message, state: FSMContext) -> None:
    role = role_for_telegram_id(message.from_user.id) if message.from_user else "client"
    menu = menu_for_user(message.from_user.id, role) if message.from_user else main_menu_keyboard()
    await edit_state_screen(
        message,
        state,
        "Отправьте описание неисправности текстом или нажмите «Отмена».",
        reply_markup=dialog_keyboard(menu),
    )


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
        ),
        parse_mode="HTML",
    )


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
