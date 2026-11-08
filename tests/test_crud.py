import pytest

from repair_bot.database.crud.requests import (
    append_request_description,
    add_request_staff_edit,
    create_repair_request,
    delete_repair_request,
    get_all_requests,
    get_repair_request,
    get_request_status_counts,
    get_user_requests,
    save_ai_assessment,
    update_request_status,
)
from repair_bot.database.crud.pricing import (
    get_urgency_tariffs,
    update_urgency_tariff,
)
from repair_bot.services.pricing.catalog import calculate_cost
from repair_bot.database.crud.users import (
    create_user,
    delete_user,
    get_all_users,
    get_user_by_telegram_id,
    update_user,
)


@pytest.mark.asyncio
async def test_user_crud(db_session):
    user = await create_user(db_session, "1001", "Иван", "+79990000001")
    assert user.role == "client"
    assert (await get_user_by_telegram_id(db_session, "1001")).id == user.id
    assert len(await get_all_users(db_session)) == 1

    updated = await update_user(
        db_session, user.id, full_name="Иван Петров", phone="+79990000002", role="master"
    )
    assert updated is not None
    assert (updated.full_name, updated.phone, updated.role) == (
        "Иван Петров",
        "+79990000002",
        "master",
    )
    assert await update_user(db_session, -1, full_name="Нет пользователя") is None

    await create_repair_request(db_session, user.id, "Проверка каскадного удаления заявки")
    assert await delete_user(db_session, user.id) is True
    assert await delete_user(db_session, user.id) is False
    assert await get_user_by_telegram_id(db_session, "1001") is None
    assert await get_all_requests(db_session) == []


@pytest.mark.asyncio
async def test_repair_request_crud_and_status_counts(db_session):
    user = await create_user(db_session, "1002", "Сергей", "+79990000003")
    request = await create_repair_request(db_session, user.id, "Компьютер не включается")

    loaded = await get_repair_request(db_session, request.id)
    assert loaded is not None
    assert loaded.user.telegram_id == "1002"
    assert loaded.urgency.label == "Обычная"
    assert loaded.urgency.surcharge_kopecks == 0
    assert len(await get_user_requests(db_session, user.id)) == 1
    assert len(await get_all_requests(db_session)) == 1
    assert (await get_request_status_counts(db_session))["new"] == 1

    updated = await update_request_status(db_session, request.id, "in_progress")
    assert updated is not None and updated.status == "in_progress"
    counts = await get_request_status_counts(db_session)
    assert counts["new"] == 0
    assert counts["in_progress"] == 1
    assert await update_request_status(db_session, -1, "completed") is None

    assert await delete_repair_request(db_session, request.id) is True
    assert await delete_repair_request(db_session, request.id) is False
    assert await get_repair_request(db_session, request.id) is None


@pytest.mark.asyncio
async def test_urgency_tariffs_and_request_snapshot(db_session):
    user = await create_user(db_session, "1005", "Клиент", "+79990000005")
    tariffs = await get_urgency_tariffs(db_session)
    assert [(item.code, item.surcharge_kopecks) for item in tariffs] == [
        ("ordinary", 0),
        ("urgent", 50_000),
        ("critical", 100_000),
    ]

    request = await create_repair_request(
        db_session, user.id, "Срочно нужен ремонт компьютера", "urgent"
    )
    updated_tariff = await update_urgency_tariff(
        db_session, "urgent", label="Срочно", surcharge_kopecks=60_000
    )
    assert updated_tariff is not None
    assert updated_tariff.surcharge_kopecks == 60_000
    saved_request = await get_repair_request(db_session, request.id)
    assert saved_request is not None
    assert saved_request.urgency.label == "Срочная"
    assert saved_request.urgency.surcharge_kopecks == 50_000

    with pytest.raises(ValueError):
        await update_urgency_tariff(
            db_session, "urgent", label="Срочная", surcharge_kopecks=-1
        )


@pytest.mark.asyncio
async def test_append_request_description_preserves_existing_text(db_session):
    user = await create_user(db_session, "1004", "Мария", "+79990000004")
    request = await create_repair_request(db_session, user.id, "Компьютер выключается сам.")

    updated = await append_request_description(
        db_session,
        request.id,
        "\n\nДополнение клиента:\nПроблема появилась после обновления Windows.",
    )

    assert updated is not None
    assert updated.description == (
        "Компьютер выключается сам.\n\nДополнение клиента:\n"
        "Проблема появилась после обновления Windows."
    )
    assert await append_request_description(db_session, -1, "Деталь") is None


@pytest.mark.asyncio
async def test_staff_edits_keep_author_and_latest_client_estimate(db_session):
    client = await create_user(db_session, "2001", "Клиент", "+79990000001")
    master = await create_user(
        db_session, "2002", "Мастер", "+79990000002", role="master"
    )
    request = await create_repair_request(db_session, client.id, "Компьютер выключается.")

    await add_request_staff_edit(
        db_session, request.id, master.id, "Проверить блок питания.", 125000
    )
    await add_request_staff_edit(
        db_session, request.id, master.id, "Заменить блок питания.", 180000
    )

    saved_request = await get_repair_request(db_session, request.id)

    assert [edit.diagnosis for edit in saved_request.staff_edits] == [
        "Проверить блок питания.",
        "Заменить блок питания.",
    ]
    assert saved_request.staff_edits[-1].client_price_kopecks == 180000
    assert saved_request.staff_edits[-1].editor_name == "Мастер"


@pytest.mark.asyncio
async def test_ai_assessment_is_saved_with_request_and_removed_by_cascade(db_session):
    user = await create_user(db_session, "1003", "AI Клиент", "+79990000004")
    request = await create_repair_request(db_session, user.id, "На экране появляется синий экран")

    assessment = await save_ai_assessment(
        db_session,
        request.id,
        priority="high",
        photo_file_id="telegram-photo-id",
        client_causes="Сбой памяти или драйвера",
        client_recommendations="Не перезагружайте компьютер много раз.",
        safety_note="Не вскрывайте устройство под напряжением.",
        master_diagnosis="Проверить память и журнал ошибок.",
        parts="Модуль памяти при подтверждении.",
        tools="Тестер памяти.",
        catalog_item_codes="diagnostics,ram_8gb",
        estimated_cost_kopecks=300000,
        client_price_kopecks=345000,
    )

    loaded = await get_repair_request(db_session, request.id)
    assert assessment is not None
    assert loaded is not None
    assert loaded.priority == "high"
    assert loaded.ai_assessment is not None
    assert loaded.ai_assessment.photo_file_id == "telegram-photo-id"

    assert await delete_user(db_session, user.id) is True
    assert await get_repair_request(db_session, request.id) is None


def test_catalog_cost_is_deterministic_and_unknown_item_has_no_price():
    items, cost, price = calculate_cost(["diagnostics", "ram_8gb"])
    assert [item.code for item in items] == ["diagnostics", "ram_8gb"]
    assert cost == 300000
    assert price == 345000
    assert calculate_cost(["missing"])[1:] == (None, None)
