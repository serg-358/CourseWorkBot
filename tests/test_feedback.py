from datetime import timedelta

import pytest

from repair_bot.database.crud.feedback import complaint_resolution, save_complaint, save_rating, update_complaint_resolution
from repair_bot.database.crud.requests import create_repair_request, take_request, update_request_status
from repair_bot.database.crud.users import create_user
from repair_bot.database.models import RepairComplaint, RepairRating, RequestCompletion, utcnow


@pytest.mark.asyncio
async def test_master_takes_request_and_client_leaves_feedback(db_session):
    client = await create_user(db_session, "81001", "Клиент", "+79990000001")
    master = await create_user(db_session, "81002", "Мастер", "+79990000002", role="master")
    item = await create_repair_request(db_session, client.id, "Не включается компьютер")

    assert await take_request(db_session, item.id, master.id)
    assert await take_request(db_session, item.id, master.id) is None
    await update_request_status(db_session, item.id, "completed")

    assert await save_rating(db_session, item.id, client.id, 5, "Спасибо")
    assert not await save_rating(db_session, item.id, client.id, 4)
    assert await save_complaint(db_session, item.id, client.id, "communication", "Нужна проверка общения")
    assert not await save_complaint(db_session, item.id, client.id, "communication", "Повтор")
    assert await db_session.get(RepairRating, item.id)
    assert await db_session.get(RepairComplaint, item.id)


@pytest.mark.asyncio
async def test_complaint_is_rejected_after_week(db_session):
    client = await create_user(db_session, "82001", "Клиент", "+79990000003")
    item = await create_repair_request(db_session, client.id, "Шумит вентилятор")
    await update_request_status(db_session, item.id, "completed")
    completion = await db_session.get(RequestCompletion, item.id)
    completion.completed_at = utcnow() - timedelta(days=8)
    await db_session.commit()

    assert not await save_complaint(db_session, item.id, client.id, "quality", "Слишком поздно")


@pytest.mark.asyncio
async def test_complaint_without_description_is_allowed_except_other(db_session):
    client = await create_user(db_session, "83001", "Клиент", "+79990000004")
    item = await create_repair_request(db_session, client.id, "Не работает экран")
    await update_request_status(db_session, item.id, "completed")

    assert await save_complaint(db_session, item.id, client.id, "quality", "")


@pytest.mark.asyncio
async def test_admin_resolves_complaint_in_order(db_session):
    client = await create_user(db_session, "84001", "Клиент", "+79990000005")
    admin = await create_user(db_session, "84002", "Администратор", "+79990000006", role="admin")
    item = await create_repair_request(db_session, client.id, "Перегревается")
    await update_request_status(db_session, item.id, "completed")
    assert await save_complaint(db_session, item.id, client.id, "quality", "Нужна проверка")

    assert await update_complaint_resolution(db_session, item.id, admin, "resolved", "") is None
    reviewing = await update_complaint_resolution(db_session, item.id, admin, "in_review", "Проверяем")
    assert reviewing and reviewing.status == "in_review"
    resolved = await update_complaint_resolution(db_session, item.id, admin, "resolved", "Вопрос решён")
    assert resolved and resolved.answer == "Вопрос решён"
    assert (await complaint_resolution(db_session, item.id)).status == "resolved"
