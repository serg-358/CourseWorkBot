"""Мягкое удаление заявок и независимое хранение фотографии."""

from sqlalchemy.ext.asyncio import AsyncSession
from repair_bot.database.models import RequestLifecycle, User, utcnow


async def set_request_photo(db: AsyncSession, request_id: int, file_id: str | None) -> None:
    """Сохраняет фото до вызова внешнего AI."""
    row = await db.get(RequestLifecycle, request_id)
    if row is None:
        row = RequestLifecycle(request_id=request_id)
        db.add(row)
    row.photo_file_id = file_id
    await db.commit()


async def move_to_trash(db: AsyncSession, request_id: int, actor: User, *, restore: bool = False) -> bool:
    """Сохраняет историю; право проверяется повторно на уровне CRUD."""
    from repair_bot.database.crud.requests import get_repair_request
    if actor.role != "admin":
        return False
    item = await get_repair_request(db, request_id, include_deleted=True)
    if item is None:
        return False
    row = item.lifecycle
    if row is None:
        row = RequestLifecycle(request_id=request_id)
        db.add(row)
    row.deleted_at = None if restore else utcnow()
    row.deleted_by_name = actor.full_name
    await db.commit()
    return True


def request_photo_id(item) -> str | None:
    """Поддерживает старые заявки с фото внутри AI-оценки."""
    lifecycle = getattr(item, "lifecycle", None)
    assessment = getattr(item, "ai_assessment", None)
    return (lifecycle.photo_file_id if lifecycle else None) or (
        assessment.photo_file_id if assessment else None
    )
