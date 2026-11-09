"""Обработчики раздела staff requests."""

import re
from decimal import Decimal, InvalidOperation

from aiogram import Bot, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from repair_bot.database.crud.requests import (
    add_request_staff_edit,
    get_all_requests,
    get_repair_request,
    update_request_status,
)
from repair_bot.database.crud.users import get_user_by_telegram_id
from repair_bot.database.session import SessionLocal
from repair_bot.keyboards import dialog_keyboard, request_navigation_keyboard
from repair_bot.handlers.common import (
    admin_request_text,
    bounded_page,
    edit_callback_screen,
    edit_state_screen,
    menu_for_user,
)
from repair_bot.states import RequestStates
from repair_bot.database.crud.request_lifecycle import request_photo_id
from repair_bot.services.request_photos import read_web_photo
from aiogram.types import BufferedInputFile
from repair_bot.services.staff_assessment import effective_staff_details
import logging
from aiogram import Router

router = Router(name="staff_requests")
logger = logging.getLogger(__name__)

@router.callback_query(F.data == "menu:all_requests")
@router.callback_query(F.data.startswith("requests:staff:"))
async def show_all_requests(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает сотруднику одну заявку на экране с листанием."""
    if callback.from_user is None:
        await callback.answer("🔒 Этот раздел доступен только мастеру или администратору.", show_alert=True)
        return
    await state.clear()

    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        role = user.role if user and user.role in {"admin", "master"} else None
        if role is None:
            await callback.answer("🔒 Нет прав для просмотра заявок.", show_alert=True)
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
                request_photo_id(request)
            ),
            can_edit_assessment=True,
        ),
        parse_mode="HTML",
    )

@router.callback_query(F.data.startswith("request:photo:"))
async def show_request_photo(callback: CallbackQuery) -> None:
    """Отправляет исходное фото только сотруднику из доверенного списка."""
    if callback.from_user is None:
        await callback.answer("🔒 Это действие доступно только мастеру или администратору.", show_alert=True)
        return
    match = re.fullmatch(r"request:photo:(\d+)", callback.data or "")
    if match is None or callback.message is None:
        await callback.answer("⚠️ Некорректное фото заявки.", show_alert=True)
        return
    async with SessionLocal() as db:
        actor = await get_user_by_telegram_id(db, str(callback.from_user.id))
        request = await get_repair_request(db, int(match.group(1)))
    if actor is None or actor.role not in {"admin", "master"}:
        await callback.answer("🔒 Нет прав для просмотра фото.", show_alert=True)
        return
    if request is None or not request_photo_id(request):
        await callback.answer("📭 Фото для этой заявки не найдено.", show_alert=True)
        return
    reference = request_photo_id(request)
    photo = reference
    if reference.startswith("web:"):
        try:
            photo = BufferedInputFile(await read_web_photo(reference), filename="repair-photo.jpg")
        except (OSError, ValueError):
            await callback.answer("Фото временно недоступно.", show_alert=True)
            return
    await callback.message.answer_photo(photo, caption=f"🖼️ Фото заявки №{request.id}")
    await callback.answer()

@router.callback_query(F.data.startswith("request:status:"))
async def change_request_status(callback: CallbackQuery, bot: Bot) -> None:
    """Разрешает мастеру и администратору обновлять статус заявки."""
    if callback.from_user is None:
        await callback.answer("🔒 Это действие доступно только мастеру или администратору.", show_alert=True)
        return
    match = re.fullmatch(r"request:status:(\d+):(in_progress|completed|cancelled)", callback.data or "")
    if not match:
        await callback.answer("⚠️ Некорректный статус заявки.", show_alert=True)
        return
    request_id, status = int(match.group(1)), match.group(2)
    async with SessionLocal() as db:
        actor = await get_user_by_telegram_id(db, str(callback.from_user.id))
        current_request = await get_repair_request(db, request_id)
        if actor is None or actor.role not in {"admin", "master"}:
            await callback.answer("🔒 Нет прав для изменения заявок.", show_alert=True)
            return
        if current_request is None:
            request = None
            previous_status = None
        else:
            previous_status = current_request.status
            await update_request_status(db, request_id, status)
            request = await get_repair_request(db, request_id)
    if request is None:
        await callback.answer("📭 Заявка не найдена.", show_alert=True)
        return
    changed = previous_status != status
    await callback.answer(
        "✅ Статус обновлён." if changed else "ℹ️ Заявка уже имеет этот статус."
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
                    request_photo_id(request)
                ),
            can_edit_assessment=True,
            ),
            parse_mode="HTML",
        )
    if not changed:
        return
    status_labels = {
        "in_progress": "🔧 Ваша заявка принята в работу.",
        "completed": "✅ Ваша заявка завершена.",
        "cancelled": "❌ Ваша заявка отменена.",
    }
    try:
        await bot.send_message(request.user.telegram_id, status_labels[status])
    except TelegramAPIError:
        # Пользователь мог заблокировать бота; смена статуса заявки уже сохранена.
        logger.info("Уведомление о заявке №%s не доставлено.", request.id)


@router.callback_query(F.data.startswith("request:edit:"))
async def start_request_staff_edit(callback: CallbackQuery, state: FSMContext) -> None:
    """Начинает проверку диагноза и цены мастером или администратором."""
    if callback.from_user is None or callback.message is None:
        return
    match = re.fullmatch(r"request:edit:(\d+)", callback.data or "")
    if match is None:
        await callback.answer("⚠️ Некорректная заявка.", show_alert=True)
        return
    request_id = int(match.group(1))
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        request = await get_repair_request(db, request_id)
    if user is None or user.role not in {"admin", "master"}:
        await callback.answer("🔒 Нет прав на исправление заявки.", show_alert=True)
        return
    if request is None:
        await callback.answer("📭 Заявка не найдена.", show_alert=True)
        return

    await state.clear()
    parts, tools = effective_staff_details(request)
    await state.update_data(
        request_id=request_id,
        staff_role=user.role,
        screen_chat_id=callback.message.chat.id,
        screen_message_id=callback.message.message_id,
        staff_parts=parts,
        staff_tools=tools,
    )
    await state.set_state(RequestStates.waiting_staff_diagnosis)
    await callback.answer()
    await edit_callback_screen(
        callback,
        f"✏️ <b>Проверка заявки №{request_id}</b>\n\nВведите подтверждённый диагноз:",
        reply_markup=dialog_keyboard(menu_for_user(callback.from_user.id, user.role)),
        parse_mode="HTML",
    )


@router.message(RequestStates.waiting_staff_diagnosis, F.text)
async def process_staff_diagnosis(message: Message, state: FSMContext) -> None:
    """Проверяет полномочия и сохраняет диагноз до шага ввода цены."""
    if message.from_user is None:
        await state.clear()
        return
    data = await state.get_data()
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(message.from_user.id))
    if user is None or user.role not in {"admin", "master"}:
        await state.clear()
        await message.answer("🔒 Нет прав для исправления заявки.")
        return
    diagnosis = (message.text or "").strip()
    if not 3 <= len(diagnosis) <= 3000:
        await edit_state_screen(
            message,
            state,
            "⚠️ Диагноз должен содержать от 3 до 3000 символов. Введите его ещё раз:",
            reply_markup=dialog_keyboard(
                menu_for_user(message.from_user.id, data.get("staff_role", "master"))
            ),
        )
        return
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(message.from_user.id))
    if user is None or user.role not in {"admin", "master"}:
        await state.clear()
        await message.answer("🔒 Нет прав на исправление заявки.")
        return
    await state.update_data(diagnosis=diagnosis)
    await state.set_state(RequestStates.waiting_staff_parts)
    await edit_state_screen(
        message,
        state,
        "🧩 Введите детали для проверки. «=» — оставить текущие, «-» — очистить.\n\n"
        + str(data.get("staff_parts", ""))[:1000],
        reply_markup=dialog_keyboard(menu_for_user(message.from_user.id, user.role)),
    )


async def process_staff_detail_field(message: Message, state: FSMContext, field: str) -> None:
    """Проверяет роль на каждом шаге редактирования деталей/инструментов."""
    if message.from_user is None:
        return
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(message.from_user.id))
    if user is None or user.role not in {"admin", "master"}:
        await state.clear()
        await message.answer("🔒 Нет прав для исправления заявки.")
        return
    value = (message.text or "").strip()
    if not value or len(value) > 3000:
        await edit_state_screen(message, state, "Введите текст до 3000 символов. «=» — оставить, «-» — очистить.", reply_markup=dialog_keyboard(menu_for_user(message.from_user.id, user.role)))
        return
    if value != "=":
        await state.update_data(**{field: "" if value == "-" else value})
    if field == "staff_parts":
        data = await state.get_data()
        await state.set_state(RequestStates.waiting_staff_tools)
        prompt = "🛠 Введите инструменты. «=» — оставить текущие, «-» — очистить.\n\n" + str(data.get("staff_tools", ""))[:1000]
    else:
        await state.set_state(RequestStates.waiting_staff_price)
        prompt = "💰 Введите подтверждённую цену для клиента в рублях (например, 2500 или 2500,50):"
    await edit_state_screen(message, state, prompt, reply_markup=dialog_keyboard(menu_for_user(message.from_user.id, user.role)))


@router.message(RequestStates.waiting_staff_parts)
async def process_staff_parts(message: Message, state: FSMContext) -> None:
    await process_staff_detail_field(message, state, "staff_parts")


@router.message(RequestStates.waiting_staff_tools)
async def process_staff_tools(message: Message, state: FSMContext) -> None:
    await process_staff_detail_field(message, state, "staff_tools")


def parse_client_price(value: str) -> int | None:
    """Преобразует введённую клиентскую цену в целое число копеек."""
    normalized = value.strip().replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d{1,8}(?:\.\d{1,2})?", normalized):
        return None
    try:
        return int(Decimal(normalized) * 100)
    except InvalidOperation:
        return None


@router.message(RequestStates.waiting_staff_price, F.text)
async def process_staff_price(message: Message, state: FSMContext) -> None:
    """Сохраняет исправление, автора и обновляет карточку в исходном сообщении."""
    if message.from_user is None:
        await state.clear()
        return
    data = await state.get_data()
    async with SessionLocal() as db:
        editor = await get_user_by_telegram_id(db, str(message.from_user.id))
    if editor is None or editor.role not in {"admin", "master"}:
        await state.clear()
        await message.answer("🔒 Нет прав для исправления заявки.")
        return
    request_id = data.get("request_id")
    diagnosis = data.get("diagnosis")
    price_kopecks = parse_client_price(message.text or "")
    if not isinstance(request_id, int) or not isinstance(diagnosis, str):
        await state.clear()
        await message.answer("ℹ️ Начните исправление заявки заново из панели мастера.")
        return
    if price_kopecks is None:
        role = data.get("staff_role", "master")
        await edit_state_screen(
            message,
            state,
            "⚠️ Введите цену числом: например, 2500 или 2500,50.",
            reply_markup=dialog_keyboard(menu_for_user(message.from_user.id, role)),
        )
        return

    async with SessionLocal() as db:
        saved_edit = await add_request_staff_edit(
            db,
            request_id,
            editor.id,
            diagnosis,
            price_kopecks,
            parts=data.get("staff_parts"),
            tools=data.get("staff_tools"),
        )
        role = editor.role
        requests = await get_all_requests(db)
    if saved_edit is None:
        await state.clear()
        await message.answer("📭 Заявка не найдена или больше недоступна.")
        return

    request = next((item for item in requests if item.id == request_id), None)
    if request is None:
        await state.clear()
        await message.answer("✅ Исправление сохранено.")
        return
    page = next(index for index, item in enumerate(requests) if item.id == request_id)
    _, total_pages = bounded_page(page, len(requests))
    await edit_state_screen(
        message,
        state,
        f"🧾 <b>Заявка {page + 1} из {len(requests)}</b>\n\n{admin_request_text(request)}",
        reply_markup=request_navigation_keyboard(
            view="staff",
            page=page,
            total_pages=total_pages,
            menu=menu_for_user(message.from_user.id, role),
            request_id=request.id,
            current_status=request.status,
            has_photo=bool(
                request_photo_id(request)
            ),
            can_edit_assessment=True,
        ),
        parse_mode="HTML",
    )
    await state.clear()
