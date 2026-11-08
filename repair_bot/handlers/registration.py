"""Обработчики раздела registration."""

import html
import hashlib
import time
from aiogram import F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from repair_bot.database.crud.requests import get_request_status_counts, get_user_requests
from repair_bot.database.crud.users import create_user, get_all_users, get_user_by_telegram_id
from repair_bot.database.crud.web import link_telegram_account
from repair_bot.database.session import SessionLocal
from repair_bot.keyboards import dialog_keyboard, main_menu_keyboard, request_navigation_keyboard
from repair_bot.states import RegistrationStates, RequestStates
from repair_bot.handlers.common import (
    CLIENT_EDITABLE_STATUSES,
    _demo_view_roles,
    _processed_start_messages,
    bounded_page,
    cancel_dialog,
    client_request_text,
    configured_role_for_telegram_id,
    dashboard_text,
    edit_callback_screen,
    edit_state_screen,
    menu_for_user,
    normalize_phone,
    role_for_telegram_id,
    safe_edit_message,
    show_start_screen,
)
import logging
from aiogram import Router

router = Router(name="registration")
logger = logging.getLogger(__name__)

@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    await cancel_dialog(message, state)


@router.message(Command("link"))
async def link_website_account(message: Message) -> None:
    """Привязывает зарегистрированный Telegram к веб-аккаунту одноразовым кодом."""
    if message.from_user is None:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not 8 <= len(parts[1].strip()) <= 64:
        await message.answer("Используйте команду из кабинета: /link одноразовый_код")
        return
    code_hash = hashlib.sha256(parts[1].strip().encode()).hexdigest()
    async with SessionLocal() as db:
        result = await link_telegram_account(db, str(message.from_user.id), code_hash)
    messages = {
        "linked": "✅ Telegram-аккаунт привязан к личному кабинету сайта.",
        "invalid": "⚠️ Код неверный или срок его действия истёк. Создайте новый в кабинете.",
        "telegram_user_missing": "Сначала зарегистрируйтесь в боте командой /start.",
        "account_already_linked": "Этот аккаунт сайта уже связан с Telegram.",
        "telegram_already_linked": "Этот Telegram уже привязан к другому аккаунту сайта.",
    }
    await message.answer(messages[result])

@router.callback_query(F.data == "dialog:cancel")
async def cancel_button(callback: CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    state_data = await state.get_data()
    await state.clear()
    if (
        current_state == RequestStates.waiting_addition.state
        and callback.from_user
        and callback.message
        and isinstance(state_data.get("request_id"), int)
    ):
        request_id = state_data["request_id"]
        async with SessionLocal() as db:
            user = await get_user_by_telegram_id(db, str(callback.from_user.id))
            requests = await get_user_requests(db, user.id) if user else []
        page = next((index for index, item in enumerate(requests) if item.id == request_id), None)
        if page is not None:
            request = requests[page]
            _, total_pages = bounded_page(page, len(requests))
            await callback.answer("↩️ Дополнение отменено.")
            await edit_callback_screen(
                callback,
                client_request_text(request),
                reply_markup=request_navigation_keyboard(
                    view="client",
                    page=page,
                    total_pages=total_pages,
                    menu=menu_for_user(callback.from_user.id, user.role),
                    request_id=request.id,
                    can_append_description=request.status in CLIENT_EDITABLE_STATUSES,
                ),
                parse_mode="HTML",
            )
            return

    await callback.answer("↩️ Действие отменено.")
    if callback.message:
        screen_chat_id = state_data.get("screen_chat_id")
        screen_message_id = state_data.get("screen_message_id")
        if screen_chat_id and screen_message_id:
            role = "client"
            if callback.from_user:
                async with SessionLocal() as db:
                    user = await get_user_by_telegram_id(db, str(callback.from_user.id))
                    if user:
                        role = user.role
            await edit_callback_screen(
                callback,
                "↩️ Действие отменено. Нажмите /start, чтобы открыть меню.",
                reply_markup=(
                    menu_for_user(callback.from_user.id, role)
                    if callback.from_user and role != "client"
                    else main_menu_keyboard("client")
                ),
            )
            return
        try:
            await callback.message.delete()
        except TelegramAPIError:
            logger.info("Не удалось удалить отменённый диалог.")

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
        expected_role = configured_role_for_telegram_id(message.from_user.id)
        if user and expected_role and user.role != expected_role:
            # .env задаёт начальные роли, дальнейшие назначения сохраняются в БД.
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
            if user.role == "admin"
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
    if callback.from_user is None:
        return
    if callback.message is None:
        await callback.answer()
        return
    await state.clear()

    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        if user is None or user.role != "admin":
            await callback.answer("🔒 Для переключения нужна роль администратора.", show_alert=True)
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
        text = await dashboard_text(db, user, role) if user else "📭 Профиль не найден. Нажмите /start."
    await callback.answer("✅ Экран обновлён.")
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
            "⚠️ Имя должно содержать минимум 3 символа. Введите имя ещё раз:",
            reply_markup=dialog_keyboard(main_menu_keyboard("client")),
        )
        return

    await state.update_data(full_name=full_name)
    await edit_state_screen(
        message,
        state,
        "📱 Ваш телефон? Например: 89999999999 или +7 (999) 999-99-99",
        reply_markup=dialog_keyboard(main_menu_keyboard("client")),
    )
    await state.set_state(RegistrationStates.waiting_phone)

@router.message(RegistrationStates.waiting_full_name)
async def process_non_text_name(message: Message, state: FSMContext) -> None:
    """Просит отправить имя текстовым сообщением."""
    await edit_state_screen(
        message,
        state,
        "📝 Пожалуйста, отправьте имя текстом.",
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
            "⚠️ Введите российский номер: 89999999999 или +7 (999) 999-99-99.",
            reply_markup=dialog_keyboard(main_menu_keyboard("client")),
        )
        return
    if message.from_user is None:
        return

    data = await state.get_data()
    full_name = data.get("full_name")
    if not full_name:
        await state.clear()
        await message.answer("⚠️ Не удалось продолжить регистрацию. Нажмите /start.")
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
            "✅ Регистрация успешна\n\n"
            "Теперь вы можете создать заявку и отслеживать её статус в разделе «Мои заявки»."
        ),
        "master": "✅ Регистрация успешна. Вы вошли в панель мастера.",
        "admin": "✅ Регистрация успешна. Вы вошли в панель администратора.",
    }
    if data.get("screen_message_id"):
        await safe_edit_message(
            message.bot,
            data["screen_chat_id"],
            data["screen_message_id"],
            registration_messages.get(user.role, "✅ Регистрация успешна"),
            reply_markup=menu_for_user(message.from_user.id, user.role),
        )
    else:
        await message.answer(
            registration_messages.get(user.role, "✅ Регистрация успешна"),
            reply_markup=menu_for_user(message.from_user.id, user.role),
        )
    await state.clear()

@router.message(RegistrationStates.waiting_phone)
async def process_non_text_phone(message: Message, state: FSMContext) -> None:
    """Просит отправить телефон текстовым сообщением."""
    await edit_state_screen(
        message,
        state,
        "📱 Пожалуйста, отправьте номер телефона текстом.",
        reply_markup=dialog_keyboard(main_menu_keyboard("client")),
    )
