"""Атомарная выдача и применение сертификатов без изменения старых таблиц."""

import secrets

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from repair_bot.database.models import GiftCertificate, RepairRequest, utcnow
from repair_bot.services.pricing.request_total import final_total


async def issue_for_request(db: AsyncSession, request_id: int) -> GiftCertificate | None:
    """Выдаёт сертификат только за завершённую и оценённую мастером заявку."""
    from repair_bot.database.crud.requests import get_repair_request
    item = await get_repair_request(db, request_id)
    if item is None or item.status != "completed" or not item.staff_edits:
        return None
    existing = await db.scalar(select(GiftCertificate).where(GiftCertificate.source_request_id == item.id))
    if existing:
        return existing
    surcharge = item.urgency.surcharge_kopecks if item.urgency else 0
    total = final_total(item, item.staff_edits[-1].client_price_kopecks, surcharge)
    if total < 1_500_000:
        return None
    gift = GiftCertificate(owner_id=item.user_id, source_request_id=item.id, source_number=item.id, qualifying_total_kopecks=total, code="PC-" + secrets.token_hex(8).upper(), amount_kopecks=100_000)
    try:
        async with db.begin_nested():
            db.add(gift)
            await db.flush()
    except IntegrityError:
        return await db.scalar(select(GiftCertificate).where(GiftCertificate.source_request_id == item.id))
    return gift


async def settle_for_status(db: AsyncSession, request_id: int, status: str) -> None:
    """При отмене освобождает резерв, при завершении фиксирует использование."""
    if status == "cancelled":
        await db.execute(update(GiftCertificate).where(GiftCertificate.target_request_id == request_id).values(target_request_id=None, used_at=None))
    elif status == "completed":
        await db.execute(update(GiftCertificate).where(GiftCertificate.target_request_id == request_id, GiftCertificate.used_at.is_(None)).values(used_at=utcnow()))
        await issue_for_request(db, request_id)


async def sync_certificates(db: AsyncSession, owner_id: int) -> None:
    """Проверяет прежние ремонты по явному запросу владельца."""
    ids = list((await db.scalars(select(RepairRequest.id).where(RepairRequest.user_id == owner_id, RepairRequest.status == "completed"))).all())
    for request_id in ids:
        await issue_for_request(db, request_id)
    await db.commit()


async def list_certificates(db: AsyncSession, owner_id: int) -> list[GiftCertificate]:
    """Возвращает только сертификаты указанного владельца."""
    return list((await db.scalars(select(GiftCertificate).where(GiftCertificate.owner_id == owner_id).order_by(GiftCertificate.created_at.desc()))).all())


async def reserve_certificate(db: AsyncSession, owner_id: int, certificate_id: int, target_id: int) -> bool:
    """Резервирует собственный сертификат за следующей открытой заявкой."""
    from repair_bot.database.crud.requests import get_repair_request
    gift = await db.scalar(select(GiftCertificate).where(GiftCertificate.id == certificate_id, GiftCertificate.owner_id == owner_id).with_for_update())
    item = await get_repair_request(db, target_id)
    if not gift or not item or item.user_id != owner_id or item.status not in {"new", "in_progress"} or not item.staff_edits:
        return False
    if gift.used_at or gift.target_request_id or item.certificate_discount or item.created_at <= gift.created_at:
        return False
    if item.staff_edits[-1].client_price_kopecks + (item.urgency.surcharge_kopecks if item.urgency else 0) <= 0:
        return False
    try:
        async with db.begin_nested():
            result = await db.execute(update(GiftCertificate).where(GiftCertificate.id == gift.id, GiftCertificate.target_request_id.is_(None), GiftCertificate.used_at.is_(None), exists(select(RepairRequest.id).where(RepairRequest.id == target_id, RepairRequest.user_id == owner_id, RepairRequest.status.in_(["new", "in_progress"])))).values(target_request_id=item.id))
            if result.rowcount != 1:
                return False
    except IntegrityError:
        return False
    await db.commit()
    return True
