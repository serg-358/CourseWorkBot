"""CRUD для оценок и жалоб по завершённым ремонтам."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repair_bot.database.models import RepairComplaint, RepairRating, RequestAssignment, RequestCompletion, RepairRequest, utcnow

COMPLAINT_CATEGORIES = {"quality", "timing", "price", "communication", "other"}


async def get_feedback_state(db: AsyncSession, request_id: int) -> tuple[int | None, bool]:
    """Возвращает оценку и признак уже отправленной жалобы по заявке."""
    rating = await db.get(RepairRating, request_id)
    complaint = await db.get(RepairComplaint, request_id)
    return (rating.score if rating else None, complaint is not None)


async def save_rating(db: AsyncSession, request_id: int, user_id: int, score: int, comment: str = "") -> bool:
    request = await db.get(RepairRequest, request_id)
    if not request or request.user_id != user_id or request.status != "completed" or not 1 <= score <= 5 or await db.get(RepairRating, request_id):
        return False
    db.add(RepairRating(request_id=request_id, score=score, comment=comment))
    await db.commit()
    return True


async def save_complaint(db: AsyncSession, request_id: int, user_id: int, category: str, message: str) -> bool:
    request = await db.get(RepairRequest, request_id)
    completion = await db.get(RequestCompletion, request_id)
    if (not request or request.user_id != user_id or request.status != "completed" or not completion
            or completion.completed_at + timedelta(days=7) < utcnow() or category not in COMPLAINT_CATEGORIES
            or (category == "other" and not message) or await db.get(RepairComplaint, request_id)):
        return False
    assignment = await db.get(RequestAssignment, request_id)
    db.add(RepairComplaint(request_id=request_id, category=category, message=message,
                           master_user_id=assignment.master_user_id if assignment else None,
                           master_name=assignment.master_name if assignment else "Не назначен"))
    await db.commit()
    return True


async def list_complaints(db: AsyncSession) -> list[RepairComplaint]:
    return list((await db.scalars(select(RepairComplaint).order_by(RepairComplaint.created_at.desc()))).all())
