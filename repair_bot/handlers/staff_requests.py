"""Обработчики раздела staff requests."""

import re
from aiogram import Bot, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from repair_bot.database.crud.requests import get_all_requests, get_repair_request, update_request_status
from repair_bot.database.crud.users import get_user_by_telegram_id
from repair_bot.database.session import SessionLocal
from repair_bot.keyboards import request_navigation_keyboard
from repair_bot.handlers.common import (
    admin_request_text,
    bounded_page,
    edit_callback_screen,
    is_staff_telegram_id,
    menu_for_user,
)
import logging
from aiogram import Router

router = Router(name="staff_requests")
logger = logging.getLogger(__name__)

@router.callback_query(F.data == "menu:all_requests")
@router.callback_query(F.data.startswith("requests:staff:"))
async def show_all_requests(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает сотруднику одну заявку на экране с листанием."""
    if callback.from_user is None or not is_staff_telegram_id(callback.from_user.id):
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
        await callback.answer("🔒 Это действие доступно только мастеру или администратору.", show_alert=True)
        return
    match = re.fullmatch(r"request:photo:(\d+)", callback.data or "")
    if match is None or callback.message is None:
        await callback.answer("⚠️ Некорректное фото заявки.", show_alert=True)
        return
    async with SessionLocal() as db:
        request = await get_repair_request(db, int(match.group(1)))
    if request is None or request.ai_assessment is None or not request.ai_assessment.photo_file_id:
        await callback.answer("📭 Фото для этой заявки не найдено.", show_alert=True)
        return
    await callback.message.answer_photo(request.ai_assessment.photo_file_id, caption=f"🖼️ Фото заявки №{request.id}")
    await callback.answer()

@router.callback_query(F.data.startswith("request:status:"))
async def change_request_status(callback: CallbackQuery, bot: Bot) -> None:
    """Разрешает мастеру и администратору обновлять статус заявки."""
    if callback.from_user is None or not is_staff_telegram_id(callback.from_user.id):
        await callback.answer("🔒 Это действие доступно только мастеру или администратору.", show_alert=True)
        return
    match = re.fullmatch(r"request:status:(\d+):(in_progress|completed|cancelled)", callback.data or "")
    if not match:
        await callback.answer("⚠️ Некорректный статус заявки.", show_alert=True)
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
                    getattr(request, "ai_assessment", None)
                    and request.ai_assessment.photo_file_id
                ),
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
