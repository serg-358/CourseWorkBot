"""Предпросмотр и атомарный импорт без AI, ролей и бонусных сертификатов."""

import json
import secrets
from datetime import datetime, timedelta
from sqlalchemy import select, update
from repair_bot.database.models import RequestImportBatch, RequestLifecycle, RepairRequest, RepairRequestAiAssessment, RepairRequestStaffEdit, RequestAddress, RequestUrgency, StaffEditDetails, User, utcnow
from repair_bot.database.crud.requests import get_all_requests
from repair_bot.services.request_transfer import identity_key, export_row, parse_transfer


async def existing_keys(db) -> set[str]:
    items = await get_all_requests(db) + await get_all_requests(db, trash=True)
    return {item.lifecycle.external_key if item.lifecycle and item.lifecycle.external_key else identity_key(export_row(item)) for item in items}


async def preview_import(db, actor: User, payload: str, kind: str) -> tuple[RequestImportBatch, list[dict], int]:
    """Хранит предпросмотр на сервере 15 минут, не в cookie браузера."""
    if actor.role != "admin":
        raise ValueError("Доступ только администратору.")
    rows = parse_transfer(payload, kind)
    known = await existing_keys(db)
    fresh = []
    for row in rows:
        key = identity_key(row)
        if key not in known:
            fresh.append(row)
            known.add(key)
    batch = RequestImportBatch(id=secrets.token_urlsafe(24), actor_id=actor.id, payload=json.dumps(fresh, ensure_ascii=False), expires_at=utcnow() + timedelta(minutes=15))
    db.add(batch)
    await db.commit()
    return batch, fresh, len(rows) - len(fresh)


async def confirm_import(db, actor: User, batch_id: str) -> int:
    """Потребляет предпросмотр и записывает всё одной транзакцией."""
    if actor.role != "admin":
        raise ValueError("Доступ только администратору.")
    payload = await db.scalar(update(RequestImportBatch).where(RequestImportBatch.id == batch_id, RequestImportBatch.actor_id == actor.id, RequestImportBatch.used_at.is_(None), RequestImportBatch.expires_at > utcnow()).values(used_at=utcnow()).returning(RequestImportBatch.payload))
    if payload is None:
        raise ValueError("Предпросмотр истёк или уже использован.")
    try:
        known = await existing_keys(db)
        count = 0
        for row in json.loads(payload):
            key = identity_key(row)
            if key in known:
                continue
            owner = await db.scalar(select(User).where(User.telegram_id == row["telegram_id"]))
            if owner is None:
                owner = User(telegram_id=row["telegram_id"], full_name=row["full_name"], phone=row["phone"], role="client")
                db.add(owner)
                await db.flush()
            item = RepairRequest(user_id=owner.id, description=row["description"], diagnosis=row["diagnosis"] or None, status=row["status"], priority=row["priority"], created_at=datetime.fromisoformat(row["created_at"]), updated_at=datetime.fromisoformat(row["updated_at"]))
            db.add(item)
            await db.flush()
            db.add(RequestLifecycle(request_id=item.id, imported=True, external_key=key, provenance=json.dumps(row, ensure_ascii=False), photo_file_id=row["photo_file_id"] or None, deleted_at=datetime.fromisoformat(row["deleted_at"]) if row["deleted_at"] else None, deleted_by_name="Импорт" if row["deleted_at"] else ""))
            if row["address"]:
                db.add(RequestAddress(request_id=item.id, address=row["address"]))
            if row["urgency"]:
                db.add(RequestUrgency(request_id=item.id, **row["urgency"]))
            if row["ai"]:
                db.add(RepairRequestAiAssessment(request_id=item.id, photo_file_id=row["photo_file_id"] or None, **row["ai"]))
            for edit in row["edits"]:
                details = StaffEditDetails(parts=edit["parts"] or "", tools=edit["tools"] or "") if edit["parts"] is not None or edit["tools"] is not None else None
                db.add(RepairRequestStaffEdit(request_id=item.id, editor_user_id=None, editor_name=edit["editor_name"], diagnosis=edit["diagnosis"], client_price_kopecks=edit["client_price_kopecks"], created_at=datetime.fromisoformat(edit["created_at"]), details=details))
            known.add(key)
            count += 1
        await db.commit()
        return count
    except Exception:
        await db.rollback()
        raise
