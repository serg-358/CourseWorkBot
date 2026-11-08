"""Обработчики раздела client requests."""

import re
from io import BytesIO
from aiogram import F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from repair_bot.database.crud.requests import append_request_description, create_repair_request, get_repair_request, get_user_requests, save_ai_assessment
from repair_bot.database.crud.users import get_user_by_telegram_id
from repair_bot.database.session import SessionLocal
from repair_bot.keyboards import (
    dialog_keyboard,
    main_menu_keyboard,
    photo_dialog_keyboard,
    request_navigation_keyboard,
    urgency_keyboard,
)
from repair_bot.keyboards.requests import format_surcharge
from repair_bot.database.crud.pricing import get_urgency_tariffs, get_urgency_tariff
from repair_bot.database.models import RepairRequest
from repair_bot.states import RequestStates
from repair_bot.services.ai.diagnostics import analyze_repair_request
from repair_bot.services.pricing.catalog import calculate_cost
from repair_bot.handlers.common import (
    CLIENT_EDITABLE_STATUSES,
    MAX_REQUEST_DESCRIPTION_LENGTH,
    REQUEST_ADDITION_PREFIX,
    bounded_page,
    client_request_text,
    edit_callback_screen,
    menu_for_user,
)
import logging
from aiogram import Router

router = Router(name="client_requests")
logger = logging.getLogger(__name__)


async def current_user_role(telegram_id: int) -> str:
    """Читает текущую роль из общей БД, включая назначения с веб-сайта."""
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(telegram_id))
    return user.role if user else "client"

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
        text = "ℹ️ Сначала зарегистрируйтесь командой /start."
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
            request_id=request.id,
            can_append_description=request.status in CLIENT_EDITABLE_STATUSES,
        )
    await edit_callback_screen(callback, text, reply_markup=markup, parse_mode="HTML")

@router.callback_query(F.data.startswith("requests:page-info"))
async def show_page_info(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
        await callback.answer(f"📄 Страница {parts[2]} из {parts[3]}")
    else:
        await callback.answer()

@router.callback_query(F.data == "menu:create")
async def start_request_creation(callback: CallbackQuery, state: FSMContext) -> None:
    """Предлагает клиенту выбрать срочность и отдельную доплату."""
    if callback.message is None or callback.from_user is None:
        return
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        tariffs = await get_urgency_tariffs(db)
    if not user:
        await callback.answer("ℹ️ Сначала зарегистрируйтесь командой /start.", show_alert=True)
        return
    await state.clear()
    await state.set_state(RequestStates.waiting_urgency)
    await callback.answer()
    await state.update_data(
        screen_chat_id=callback.message.chat.id,
        screen_message_id=callback.message.message_id,
    )
    await edit_callback_screen(
        callback,
        "⏱ <b>Выберите срочность ремонта</b>\n"
        "Доплата за срочность показана отдельно от стоимости ремонта.",
        reply_markup=urgency_keyboard(tariffs),
        parse_mode="HTML",
    )


@router.callback_query(RequestStates.waiting_urgency, F.data.startswith("urgency:choose:"))
async def choose_request_urgency(callback: CallbackQuery, state: FSMContext) -> None:
    """Сохраняет выбранный тариф в FSM и открывает ввод описания."""
    if callback.from_user is None:
        return
    code = (callback.data or "").rsplit(":", 1)[-1]
    async with SessionLocal() as db:
        tariff = await get_urgency_tariff(db, code)
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
    if tariff is None or user is None:
        await callback.answer("Тариф недоступен. Откройте создание заявки снова.", show_alert=True)
        return
    await state.update_data(urgency_code=tariff.code)
    await state.set_state(RequestStates.waiting_description)
    await callback.answer()
    await edit_callback_screen(
        callback,
        f"⏱ Срочность: <b>{tariff.label}</b> ({format_surcharge(tariff.surcharge_kopecks)}).\n\n"
        "📝 Теперь опишите неисправность компьютера. Мастер увидит это описание в заявке.",
        reply_markup=dialog_keyboard(menu_for_user(callback.from_user.id, user.role)),
        parse_mode="HTML",
    )

@router.callback_query(F.data.startswith("request:append:"))
async def start_request_addition(callback: CallbackQuery, state: FSMContext) -> None:
    """Начинает добавление клиентом деталей к собственной открытой заявке."""
    if callback.from_user is None or callback.message is None:
        return
    match = re.fullmatch(r"request:append:(\d+)", callback.data or "")
    if match is None:
        await callback.answer("⚠️ Некорректная заявка.", show_alert=True)
        return
    request_id = int(match.group(1))
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(callback.from_user.id))
        request = await get_repair_request(db, request_id)
    if user is None or request is None or request.user_id != user.id:
        await callback.answer("📭 Заявка не найдена.", show_alert=True)
        return
    if request.status not in CLIENT_EDITABLE_STATUSES:
        await callback.answer("⚠️ Эту заявку уже нельзя дополнить.", show_alert=True)
        return
    await state.clear()
    await state.update_data(request_id=request_id)
    await state.set_state(RequestStates.waiting_addition)
    await callback.answer()
    await edit_callback_screen(
        callback,
        "✏️ Напишите детали, которые хотите добавить к описанию заявки.",
        reply_markup=dialog_keyboard(menu_for_user(callback.from_user.id, user.role)),
    )

@router.message(RequestStates.waiting_description, F.text)
async def process_request_description(message: Message, state: FSMContext) -> None:
    """Принимает описание и переходит к необязательному фото ошибки."""
    if message.from_user is None:
        return
    description = (message.text or "").strip()
    if len(description) < 10:
        role = await current_user_role(message.from_user.id)
        menu = menu_for_user(message.from_user.id, role)
        await message.answer(
            "⚠️ Опишите проблему подробнее (не менее 10 символов). Попробуйте ещё раз:",
            reply_markup=dialog_keyboard(menu),
        )
        return
    await state.update_data(description=description[:4000])
    await state.set_state(RequestStates.waiting_photo)
    role = await current_user_role(message.from_user.id)
    await message.answer(
        "🤖 Для предварительной AI-оценки описание и необязательное фото ошибки "
        "будут переданы в OpenAI. Пришлите одно фото до 10 МБ или нажмите «Пропустить фото».",
        reply_markup=photo_dialog_keyboard(menu_for_user(message.from_user.id, role)),
    )

@router.message(RequestStates.waiting_description)
async def process_non_text_description(message: Message, state: FSMContext) -> None:
    role = (
        await current_user_role(message.from_user.id)
        if message.from_user
        else "client"
    )
    menu = menu_for_user(message.from_user.id, role) if message.from_user else main_menu_keyboard()
    await message.answer(
        "📝 Отправьте описание неисправности текстом или нажмите «Отмена».",
        reply_markup=dialog_keyboard(menu),
    )

@router.message(RequestStates.waiting_addition, F.text)
async def process_request_addition(message: Message, state: FSMContext) -> None:
    """Проверяет и сохраняет дополнение к заявке клиента."""
    if message.from_user is None:
        return
    addition = (message.text or "").strip()
    if not addition:
        await message.answer("⚠️ Отправьте непустое текстовое дополнение или нажмите «Отмена».")
        return
    data = await state.get_data()
    request_id = data.get("request_id")
    if not isinstance(request_id, int):
        await state.clear()
        await message.answer("ℹ️ Начните добавление деталей заново через «Мои заявки».")
        return

    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(message.from_user.id))
        request = await get_repair_request(db, request_id)
        if user is None or request is None or request.user_id != user.id:
            saved_request = None
            requests = []
        elif request.status not in CLIENT_EDITABLE_STATUSES:
            saved_request = None
            requests = []
        else:
            remaining = (
                MAX_REQUEST_DESCRIPTION_LENGTH
                - len(request.description)
                - len(REQUEST_ADDITION_PREFIX)
            )
            if len(addition) > remaining:
                await message.answer(
                    "⚠️ Дополнение не сохранено: доступно символов: "
                    f"{max(0, remaining)}."
                )
                return
            saved_request = await append_request_description(
                db,
                request.id,
                f"{REQUEST_ADDITION_PREFIX}{addition}",
            )
            if saved_request is not None:
                saved_request = await get_repair_request(db, saved_request.id)
            requests = await get_user_requests(db, user.id)

    if saved_request is None:
        await state.clear()
        await message.answer("⚠️ Эту заявку уже нельзя дополнить. Откройте «Мои заявки».")
        return

    await state.clear()
    page = next((index for index, item in enumerate(requests) if item.id == saved_request.id), 0)
    _, total_pages = bounded_page(page, len(requests))
    await message.answer(
        f"✅ Дополнение к заявке №{saved_request.id} сохранено.\n\n"
        f"{client_request_text(saved_request)}",
        reply_markup=request_navigation_keyboard(
            view="client",
            page=page,
            total_pages=total_pages,
            menu=menu_for_user(message.from_user.id, user.role),
            request_id=saved_request.id,
            can_append_description=True,
        ),
        parse_mode="HTML",
    )

@router.message(RequestStates.waiting_addition)
async def process_non_text_addition(message: Message) -> None:
    """Не даёт сохранить вложения вместо текстового уточнения."""
    await message.answer("📝 Отправьте детали текстом или нажмите «Отмена».")

async def create_and_analyze_request(
    telegram_id: int,
    description: str,
    photo_file_id: str | None = None,
    photo_bytes: bytes | None = None,
    urgency_code: str = "ordinary",
) -> tuple[RepairRequest | None, str]:
    """Надёжно создаёт заявку, затем пытается дополнить её AI-оценкой."""
    async with SessionLocal() as db:
        user = await get_user_by_telegram_id(db, str(telegram_id))
        if user is None:
            return None, "client"
        request = await create_repair_request(db, user.id, description, urgency_code)
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
        await message.answer("ℹ️ Начните создание заявки заново.")
        return
    request, role = await create_and_analyze_request(
        actor_id,
        description,
        photo_file_id,
        photo_bytes,
        data.get("urgency_code", "ordinary"),
    )
    await state.clear()
    if request is None:
        await message.answer("ℹ️ Сначала зарегистрируйтесь командой /start.")
        return
    text = f"✅ Заявка №{request.id} создана.\n\n{client_request_text(request)}"
    if getattr(request, "ai_assessment", None) is None:
        text += (
            "\n\n⚠️ Предварительная AI-оценка сейчас недоступна. "
            "Заявка передана мастеру, который уточнит причину и стоимость."
        )
    await message.answer(text, reply_markup=menu_for_user(actor_id, role), parse_mode="HTML")

async def delete_ai_progress_message(progress_message: Message) -> None:
    """Удаляет временный индикатор обработки, не прерывая сценарий заявки."""
    try:
        await progress_message.delete()
    except TelegramAPIError:
        logger.info("Не удалось удалить сообщение об ИИ-оценке заявки.")

@router.callback_query(RequestStates.waiting_photo, F.data == "ai:skip-photo")
async def skip_request_photo(callback: CallbackQuery, state: FSMContext) -> None:
    """Создаёт заявку без изображения."""
    if callback.from_user is None or callback.message is None:
        return
    await callback.answer()
    progress_message = await callback.message.answer("🤖 Проводим ИИ-оценку заявки…")
    try:
        await finish_request_creation(callback.message, state, telegram_id=callback.from_user.id)
    finally:
        await delete_ai_progress_message(progress_message)

@router.message(RequestStates.waiting_photo, F.photo)
async def process_request_photo(message: Message, state: FSMContext) -> None:
    """Принимает одно фото ошибки и передаёт его AI вместе с описанием."""
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > 10 * 1024 * 1024:
        await message.answer("⚠️ Фото больше 10 МБ. Пришлите меньшее фото или пропустите этот шаг.")
        return
    progress_message = await message.answer("🤖 Проводим ИИ-оценку заявки…")
    try:
        try:
            telegram_file = await message.bot.get_file(photo.file_id)
            downloaded = await message.bot.download_file(telegram_file.file_path)
            photo_bytes = downloaded.getvalue() if isinstance(downloaded, BytesIO) else None
        except TelegramAPIError:
            await message.answer("⚠️ Не удалось получить фото. Попробуйте ещё раз или пропустите этот шаг.")
            return
        await finish_request_creation(
            message, state, photo_file_id=photo.file_id, photo_bytes=photo_bytes
        )
    finally:
        await delete_ai_progress_message(progress_message)

@router.message(RequestStates.waiting_photo)
async def process_invalid_request_photo(message: Message) -> None:
    await message.answer("🖼️ Пришлите обычное фото ошибки на экране или нажмите «Пропустить фото».")
