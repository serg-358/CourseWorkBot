"""CRUD-операции для заявок на ремонт."""

from repair_bot.services.request_priority import PRIORITY_BY_URGENCY

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from repair_bot.database.crud.pricing import get_urgency_tariff
from repair_bot.database.models import (
    RepairRequest,
    RepairRequestAiAssessment,
    RepairRequestStaffEdit,
    RequestUrgency,
    User,
    GiftCertificate,
    StaffEditDetails,
    RequestLifecycle,
)


async def get_request_status_counts(db: AsyncSession) -> dict[str, int]:
    """Возвращает количество заявок по статусам одним запросом к базе."""
    result = await db.execute(
        select(RepairRequest.status, func.count(RepairRequest.id)).where(
            ~RepairRequest.lifecycle.has(RequestLifecycle.deleted_at.is_not(None))
        ).group_by(
            RepairRequest.status
        )
    )
    counts = {status: count for status, count in result.all()}
    return {
        "new": counts.get("new", 0),
        "in_progress": counts.get("in_progress", 0),
        "completed": counts.get("completed", 0),
        "cancelled": counts.get("cancelled", 0),
    }


async def create_repair_request(
    db: AsyncSession, user_id: int, description: str, urgency_code: str = "ordinary", *, photo_file_id: str | None = None
) -> RepairRequest:
    """Создаёт заявку и сохраняет снимок выбранной доплаты за срочность."""
    tariff = await get_urgency_tariff(db, urgency_code)
    if tariff is None:
        raise ValueError("Выбранный тариф срочности недоступен.")
    request = RepairRequest(user_id=user_id, description=description, priority=PRIORITY_BY_URGENCY[urgency_code])
    if photo_file_id:
        request.lifecycle = RequestLifecycle(photo_file_id=photo_file_id)
    db.add(request)
    await db.flush()
    db.add(
        RequestUrgency(
            request_id=request.id,
            tariff_code=tariff.code,
            label=tariff.label,
            surcharge_kopecks=tariff.surcharge_kopecks,
        )
    )
    await db.commit()
    await db.refresh(request)
    return request


async def get_repair_request(db: AsyncSession, request_id: int, *, include_deleted: bool = False) -> RepairRequest | None:
    """Возвращает заявку по первичному ключу вместе с владельцем."""
    result = await db.execute(
        select(RepairRequest)
        .options(
            joinedload(RepairRequest.user),
            joinedload(RepairRequest.ai_assessment),
            joinedload(RepairRequest.urgency),
            selectinload(RepairRequest.staff_edits).joinedload(
                RepairRequestStaffEdit.editor
            ),
        )
        .where(RepairRequest.id == request_id, True if include_deleted else ~RepairRequest.lifecycle.has(RequestLifecycle.deleted_at.is_not(None)))
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_user_requests(db: AsyncSession, user_id: int) -> list[RepairRequest]:
    """Возвращает заявки пользователя от новых к старым."""
    result = await db.execute(
        select(RepairRequest)
        .options(
            joinedload(RepairRequest.ai_assessment),
            joinedload(RepairRequest.urgency),
            selectinload(RepairRequest.staff_edits).joinedload(
                RepairRequestStaffEdit.editor
            ),
        )
        .where(RepairRequest.user_id == user_id, ~RepairRequest.lifecycle.has(RequestLifecycle.deleted_at.is_not(None)))
        .order_by(RepairRequest.created_at.desc(), RepairRequest.id.desc())
    )
    return list(result.scalars().all())


async def get_all_requests(db: AsyncSession, *, trash: bool = False) -> list[RepairRequest]:
    """Возвращает заявки вместе с данными их владельцев."""
    result = await db.execute(
        select(RepairRequest)
        .options(
            joinedload(RepairRequest.user),
            joinedload(RepairRequest.ai_assessment),
            joinedload(RepairRequest.urgency),
            selectinload(RepairRequest.staff_edits).joinedload(
                RepairRequestStaffEdit.editor
            ),
        )
        .where(RepairRequest.lifecycle.has(RequestLifecycle.deleted_at.is_not(None)) if trash else ~RepairRequest.lifecycle.has(RequestLifecycle.deleted_at.is_not(None)))
        .order_by(RepairRequest.created_at.desc(), RepairRequest.id.desc())
    )
    return list(result.scalars().all())


async def update_request_status(
    db: AsyncSession, request_id: int, status: str
) -> RepairRequest | None:
    """Меняет статус заявки и обновляет дату изменения."""
    request = await db.get(RepairRequest, request_id)
    if request is None:
        return None
    request.status = status
    await db.flush()
    from repair_bot.database.crud.certificates import settle_for_status
    await settle_for_status(db, request_id, status)
    await db.commit()
    await db.refresh(request)
    return request


async def append_request_description(
    db: AsyncSession, request_id: int, addition: str
) -> RepairRequest | None:
    """Дописывает подготовленное уточнение к описанию заявки."""
    request = await db.get(RepairRequest, request_id)
    if request is None:
        return None
    request.description = f"{request.description}{addition}"
    await db.commit()
    await db.refresh(request)
    return request


async def save_ai_assessment(
    db: AsyncSession,
    request_id: int,
    *,
    priority: str,
    photo_file_id: str | None,
    client_causes: str,
    client_recommendations: str,
    safety_note: str,
    master_diagnosis: str,
    parts: str,
    tools: str,
    catalog_item_codes: str,
    estimated_cost_kopecks: int | None,
    client_price_kopecks: int | None,
) -> RepairRequestAiAssessment | None:
    """Сохраняет AI-оценку; старый аргумент priority не меняет выбор клиента."""
    request = await db.get(RepairRequest, request_id)
    if request is None:
        return None
    assessment = RepairRequestAiAssessment(
        request_id=request_id,
        photo_file_id=photo_file_id,
        client_causes=client_causes,
        client_recommendations=client_recommendations,
        safety_note=safety_note,
        master_diagnosis=master_diagnosis,
        parts=parts,
        tools=tools,
        catalog_item_codes=catalog_item_codes,
        estimated_cost_kopecks=estimated_cost_kopecks,
        client_price_kopecks=client_price_kopecks,
    )
    db.add(assessment)
    await db.commit()
    await db.refresh(assessment)
    return assessment


async def add_request_staff_edit(
    db: AsyncSession,
    request_id: int,
    editor_user_id: int,
    diagnosis: str,
    client_price_kopecks: int,
    *,
    parts: str | None = None,
    tools: str | None = None,
) -> RepairRequestStaffEdit | None:
    """Добавляет подтверждённую правку и сохраняет автора для аудита."""
    request = await get_repair_request(db, request_id)
    editor = await db.get(User, editor_user_id)
    if request is None or editor is None or editor.role not in {"admin", "master"}:
        return None
    from repair_bot.services.staff_assessment import effective_staff_details
    previous_parts, previous_tools = effective_staff_details(request)
    parts = previous_parts if parts is None else parts
    tools = previous_tools if tools is None else tools
    if len(parts) > 3000 or len(tools) > 3000:
        raise ValueError("Детали и инструменты должны содержать не более 3000 символов.")
    edit = RepairRequestStaffEdit(
        request_id=request_id,
        editor_user_id=editor_user_id,
        editor_name=editor.full_name,
        diagnosis=diagnosis,
        client_price_kopecks=client_price_kopecks,
        details=StaffEditDetails(parts=parts, tools=tools),
    )
    db.add(edit)
    await db.flush()
    if request.status == "completed":
        from repair_bot.database.crud.certificates import issue_for_request
        await issue_for_request(db, request_id)
    await db.commit()
    await db.refresh(edit)
    return edit


async def delete_repair_request(db: AsyncSession, request_id: int) -> bool:
    """Удаляет заявку; возвращает False, если запись не найдена."""
    request = await db.get(RepairRequest, request_id)
    if request is None:
        return False
    await db.execute(update(GiftCertificate).where(GiftCertificate.source_request_id == request_id).values(source_request_id=None))
    await db.execute(update(GiftCertificate).where(GiftCertificate.target_request_id == request_id).values(target_request_id=None))
    await db.delete(request)
    await db.commit()
    return True
