"""CRUD-операции для заявок на ремонт."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from models import RepairRequest


async def get_request_status_counts(db: AsyncSession) -> dict[str, int]:
    """Возвращает количество заявок по статусам одним запросом к базе."""
    result = await db.execute(
        select(RepairRequest.status, func.count(RepairRequest.id)).group_by(
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
    db: AsyncSession, user_id: int, description: str
) -> RepairRequest:
    """Создаёт заявку, связанную с зарегистрированным пользователем."""
    request = RepairRequest(user_id=user_id, description=description)
    db.add(request)
    await db.commit()
    await db.refresh(request)
    return request


async def get_repair_request(db: AsyncSession, request_id: int) -> RepairRequest | None:
    """Возвращает заявку по первичному ключу вместе с владельцем."""
    result = await db.execute(
        select(RepairRequest)
        .options(joinedload(RepairRequest.user))
        .where(RepairRequest.id == request_id)
    )
    return result.scalar_one_or_none()


async def get_user_requests(db: AsyncSession, user_id: int) -> list[RepairRequest]:
    """Возвращает заявки пользователя от новых к старым."""
    result = await db.execute(
        select(RepairRequest)
        .where(RepairRequest.user_id == user_id)
        .order_by(RepairRequest.created_at.desc(), RepairRequest.id.desc())
    )
    return list(result.scalars().all())


async def get_all_requests(db: AsyncSession) -> list[RepairRequest]:
    """Возвращает заявки вместе с данными их владельцев."""
    result = await db.execute(
        select(RepairRequest)
        .options(joinedload(RepairRequest.user))
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
    await db.commit()
    await db.refresh(request)
    return request


async def delete_repair_request(db: AsyncSession, request_id: int) -> bool:
    """Удаляет заявку; возвращает False, если запись не найдена."""
    request = await db.get(RepairRequest, request_id)
    if request is None:
        return False
    await db.delete(request)
    await db.commit()
    return True
