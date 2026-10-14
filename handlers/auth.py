"""Обработчики запуска и регистрации пользователя."""

import html
import os
import logging
import re
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from crud.requests import (
    create_repair_request,
    get_all_requests,
    get_request_status_counts,
    get_user_requests,
    update_request_status,
)
from crud.users import create_user, get_all_users, get_user_by_telegram_id
from database import SessionLocal
from keyboards import cancel_keyboard, main_menu_keyboard, request_status_keyboard
from models import RepairRequest
from states import RegistrationStates, RequestStates

router = Router(name="auth")
logger = logging.getLogger(__name__)

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
    return (
        f"Заявка №{request.id}\nПользователь: {request.user.full_name}\n"
        f"Телефон: {request.user.phone}\n"
        f"Приоритет: {priority_labels.get(request.priority, request.priority)}\n"
        f"Статус: {status_labels.get(request.status, request.status)}\n\n"
        f"{request.description[:2500]}"
    )


async def cancel_dialog(message: Message, state: FSMContext) -> None:
    """Сбрасывает активную регистрацию или создание заявки."""
    await state.clear()
    await message.answer(
        "Действие отменено. Нажмите /start, чтобы открыть меню."
    )


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    await cancel_dialog(message, state)


@router.callback_query(F.data == "dialog:cancel")
async def cancel_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Действие отменено.")
    if callback.message:
        await callback.message.edit_text("Действие отменено. Нажмите /start, чтобы открыть меню.")


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
        await message.answer(
            welcome_text,
            reply_markup=menu_for_user(message.from_user.id, user.role),
            parse_mode="HTML",
        )
        return

    await message.answer(
        "🖥 <b>ПК Доктор</b>\nПомогаем оформить заявку на ремонт компьютера.\n\n"
        "Опишите неисправность, а статус заявки сможете проверять в боте.\n\n"
        "Для начала создадим профиль. Как вас зовут?",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(RegistrationStates.waiting_full_name)


@router.callback_query(F.data.in_({"view:admin", "view:master", "view:client", "demo:toggle"}))
async def switch_demo_view(callback: CallbackQuery) -> None:
    """Переключает демонстрационные панели только для владельца-администратора."""
    if callback.from_user is None or not is_demo_switch_user(callback.from_user.id):
        await callback.answer("Переключение доступно только владельцу бота.", show_alert=True)
        return

    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        if user is None or user.role != "admin":
            await callback.answer("Для переключения нужна роль администратора.", show_alert=True)
            return

    requested_view = "client" if callback.data == "demo:toggle" else (callback.data or "").split(":", 1)[-1]
    if requested_view == "admin":
        _demo_view_roles.pop(callback.from_user.id, None)
        text, role = "Открыта панель администратора.", "admin"
    else:
        _demo_view_roles[callback.from_user.id] = requested_view
        if requested_view == "master":
            text, role = "Открыта демонстрационная панель мастера.", "master"
        else:
            text, role = "Открыт клиентский интерфейс для демонстрации.", "client"

    await callback.answer(text)
    if callback.message:
        await callback.message.answer(
            text,
            reply_markup=main_menu_keyboard(
                role,
                demo_role=None if role == "admin" else role,
                allow_role_switch=True,
            ),
        )


@router.message(RegistrationStates.waiting_full_name, F.text)
async def process_full_name(message: Message, state: FSMContext) -> None:
    """Проверяет имя, сохраняет его в FSM и запрашивает телефон."""
    full_name = (message.text or "").strip()
    if len(full_name) < 3:
        await message.answer("Имя должно содержать минимум 3 символа. Введите имя ещё раз:")
        return

    await state.update_data(full_name=full_name)
    await message.answer(
        "Ваш телефон? Например: 89999999999 или +7 (999) 999-99-99",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(RegistrationStates.waiting_phone)


@router.message(RegistrationStates.waiting_full_name)
async def process_non_text_name(message: Message) -> None:
    """Просит отправить имя текстовым сообщением."""
    await message.answer("Пожалуйста, отправьте имя текстом.")


@router.message(RegistrationStates.waiting_phone, F.text)
async def process_phone(message: Message, state: FSMContext) -> None:
    """Проверяет телефон, создаёт пользователя и показывает меню."""
    phone = normalize_phone(message.text or "")
    if phone is None:
        await message.answer(
            "Введите российский номер: 89999999999 или +7 (999) 999-99-99."
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

    await state.clear()
    registration_messages = {
        "client": (
            "Регистрация успешна ✅\n\n"
            "Теперь вы можете создать заявку и отслеживать её статус в разделе «Мои заявки»."
        ),
        "master": "Регистрация успешна ✅ Вы вошли в панель мастера.",
        "admin": "Регистрация успешна ✅ Вы вошли в панель администратора.",
    }
    await message.answer(
        registration_messages.get(user.role, "Регистрация успешна ✅"),
        reply_markup=menu_for_user(message.from_user.id, user.role),
    )


@router.message(RegistrationStates.waiting_phone)
async def process_non_text_phone(message: Message) -> None:
    """Просит отправить телефон текстовым сообщением."""
    await message.answer("Пожалуйста, отправьте номер телефона текстом.")


@router.callback_query(F.data == "menu:requests")
async def show_my_requests(callback: CallbackQuery) -> None:
    """Показывает заявки текущего пользователя."""
    await callback.answer()
    if callback.message is None or callback.from_user is None:
        return
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        requests = await get_user_requests(db, user.id) if user else []
        role = user.role if user else "client"
    if not user:
        await callback.message.answer("Сначала зарегистрируйтесь командой /start.")
    elif not requests:
        await callback.message.answer("У вас пока нет заявок.", reply_markup=menu_for_user(callback.from_user.id, role))
    else:
        for request in requests[:10]:
            status_labels = {
                "new": "Новая",
                "in_progress": "В работе",
                "completed": "Завершена",
                "cancelled": "Отменена",
            }
            priority_labels = {"low": "Низкий", "medium": "Средний", "high": "Высокий"}
            await callback.message.answer(
                f"Заявка №{request.id}\n"
                f"Статус: {status_labels.get(request.status, request.status)}\n"
                f"Приоритет: {priority_labels.get(request.priority, request.priority)}\n"
                f"Описание: {request.description[:2500]}"
            )
        await callback.message.answer("Это ваши последние заявки.", reply_markup=menu_for_user(callback.from_user.id, role))


@router.callback_query(F.data == "menu:create")
async def start_request_creation(callback: CallbackQuery, state: FSMContext) -> None:
    """Начинает ввод описания новой заявки."""
    await callback.answer()
    if callback.message is None or callback.from_user is None:
        return
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        role = user.role if user else "client"
    if not user:
        await callback.message.answer("Сначала зарегистрируйтесь командой /start.")
        return
    await state.clear()
    await state.set_state(RequestStates.waiting_description)
    await callback.message.answer(
        "Опишите неисправность компьютера. Мастер увидит это описание в заявке.",
        reply_markup=cancel_keyboard(),
    )


@router.message(RequestStates.waiting_description, F.text)
async def process_request_description(message: Message, state: FSMContext) -> None:
    """Сохраняет заявку и связывает её с авторизованным пользователем."""
    if message.from_user is None:
        return
    description = (message.text or "").strip()
    if len(description) < 10:
        await message.answer("Опишите проблему подробнее (не менее 10 символов) или нажмите «Отмена».")
        return
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(message.from_user.id))
        if user is None:
            await state.clear()
            await message.answer("Сначала зарегистрируйтесь командой /start.")
            return
        request = await create_repair_request(db, user.id, description[:4000])
        role = user.role
    await state.clear()
    await message.answer(
        f"Заявка №{request.id} создана. Текущий статус: {request.status}.",
        reply_markup=menu_for_user(message.from_user.id, role),
    )


@router.message(RequestStates.waiting_description)
async def process_non_text_description(message: Message) -> None:
    await message.answer("Отправьте описание неисправности текстом или нажмите «Отмена».")


@router.callback_query(F.data == "menu:all_requests")
async def show_all_requests(callback: CallbackQuery) -> None:
    """Показывает мастеру или администратору заявки и кнопки смены статуса."""
    if callback.from_user is None or not is_staff_telegram_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только мастеру или администратору.", show_alert=True)
        return

    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        role = user.role if user and user.role in {"admin", "master"} else None
        if role is None:
            await callback.answer("Нет прав для просмотра заявок.", show_alert=True)
            return
        requests = await get_all_requests(db)

    await callback.answer()
    if callback.message is None:
        return
    if not requests:
        await callback.message.answer("Заявок пока нет.", reply_markup=menu_for_user(callback.from_user.id, role))
        return
    for request in requests[:10]:
        await callback.message.answer(
            admin_request_text(request),
            reply_markup=request_status_keyboard(request.id, request.status),
        )
    await callback.message.answer("Показаны последние 10 заявок.", reply_markup=menu_for_user(callback.from_user.id, role))


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
        current_request = await db.get(RepairRequest, request_id)
        if current_request is None:
            request = None
            previous_status = None
        else:
            previous_status = current_request.status
            request = await update_request_status(db, request_id, status)
        if request:
            # Загружаем карточку заново с владельцем для обновления текста.
            requests = await get_all_requests(db)
            request = next((item for item in requests if item.id == request_id), None)
    if request is None:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if previous_status == status:
        await callback.answer("Заявка уже имеет этот статус.")
        return
    await callback.answer("Статус обновлён.")
    if callback.message:
        await callback.message.edit_text(
            admin_request_text(request),
            reply_markup=request_status_keyboard(request.id, request.status),
        )
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
async def show_users(callback: CallbackQuery) -> None:
    """Показывает администратору пользователей выбранной роли."""
    if callback.from_user is None or not is_admin_telegram_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только администратору.", show_alert=True)
        return

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
        text = empty_labels.get(role_filter, "Пользователей пока нет.")
    else:
        titles = {"client": "Клиенты:", "master": "Мастера:"}
        lines = [titles.get(role_filter, "Все пользователи:")]
        for user in users[:20]:
            lines.append(
                f"\n{user.full_name[:60]} — {user.role}\n"
                f"Телефон: {user.phone[:20]}\nTelegram ID: {user.telegram_id[:24]}"
            )
        if len(users) > 20:
            lines.append(f"\nПоказаны первые 20 из {len(users)} пользователей.")
        text = "\n".join(lines)

    await callback.message.answer(text, reply_markup=menu_for_user(callback.from_user.id, "admin"))


@router.callback_query(F.data == "menu:admins")
async def show_administrators(callback: CallbackQuery) -> None:
    """Показывает отдельный список аккаунтов с ролью администратора."""
    if callback.from_user is None or not is_admin_telegram_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только администратору.", show_alert=True)
        return

    async with SessionLocal() as db:
        current_user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        users = await get_all_users(db) if current_user and current_user.role == "admin" else None

    if users is None:
        await callback.answer("Нет прав для просмотра раздела.", show_alert=True)
        return

    admins = [user for user in users if user.role == "admin"]
    lines = [f"Администраторы системы: {len(admins)}"]
    lines.extend(f"\n🛡 {user.full_name}" for user in admins)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "\n".join(lines),
            reply_markup=menu_for_user(callback.from_user.id, "admin"),
        )


@router.callback_query(F.data == "menu:statistics")
async def show_statistics(callback: CallbackQuery) -> None:
    """Показывает доступную сейчас статистику по зарегистрированным пользователям."""
    if callback.from_user is None or not is_admin_telegram_id(callback.from_user.id):
        await callback.answer("Этот раздел доступен только администратору.", show_alert=True)
        return

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
        await callback.message.answer(
            f"Статистика пользователей:\nВсего: {len(users)}\n"
            f"Клиентов: {clients}\nМастеров: {sum(user.role == 'master' for user in users)}\n"
            f"Администраторов: {admins}\n\n"
            f"Заявок всего: {len(requests)}\nНовых: {statuses['new']}\n"
            f"В работе: {statuses['in_progress']}\nЗавершено: {statuses['completed']}\n"
            f"Отменено: {statuses['cancelled']}",
            reply_markup=menu_for_user(callback.from_user.id, "admin"),
        )


@router.callback_query(F.data == "menu:profile")
async def show_profile(callback: CallbackQuery) -> None:
    """Показывает профиль и актуализирует роль администратора."""
    await callback.answer()
    if callback.from_user is None:
        return
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
            await callback.message.answer(
                "Профиль не найден. Нажмите /start для регистрации.",
                reply_markup=main_menu_keyboard(),
            )
        return

    if callback.message:
        await callback.message.answer(
            f"Ваш профиль:\nИмя: {user.full_name}\nТелефон: {user.phone}\n"
            f"Роль в системе: {user.role}"
            + (
                f"\nРежим показа: {_demo_view_roles[callback.from_user.id]}"
                if callback.from_user.id in _demo_view_roles
                else ""
            ),
            reply_markup=menu_for_user(callback.from_user.id, user.role),
        )
