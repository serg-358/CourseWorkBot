"""Проверки квот, привязки, корзины, редактирования и переноса на временной БД."""

import asyncio
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

import repair_bot.web.app as web
from repair_bot.database.crud.users import create_user
from repair_bot.database.crud.requests import create_repair_request, get_repair_request, get_all_requests, get_user_requests, add_request_staff_edit
from repair_bot.database.crud.request_lifecycle import move_to_trash, set_request_photo, request_photo_id
from repair_bot.database.crud.ai_access import reserve_analysis, finish_analysis, get_wallet, grant_analysis
from repair_bot.database.crud.telegram_codes import issue_telegram_code, verify_telegram_code, code_digest
from repair_bot.database.crud.request_transfer import preview_import, confirm_import
from repair_bot.database.crud.certificates import issue_for_request
from repair_bot.database.models import Base, AiUsage, GiftCertificate, RepairRequest, RepairRequestAiAssessment, RequestImportBatch, TelegramAccessCode, User, WebsiteAccount, utcnow
from repair_bot.services.ai.budget import analyze_with_budget
from repair_bot.services.request_transfer import export_row, parse_transfer, serialize_rows
from repair_bot.services.staff_assessment import effective_staff_details


async def people(db):
    client = await create_user(db, "95501", "Клиент Тест", "+79990000001")
    admin = await create_user(db, "95502", "Админ Тест", "+79990000002", "admin")
    return client, admin


def assessment(request_id):
    return RepairRequestAiAssessment(request_id=request_id, client_causes="Причина", client_recommendations="Совет", safety_note="Безопасность", master_diagnosis="Исходный диагноз", parts="Исходные детали", tools="Исходные инструменты", catalog_item_codes="", estimated_cost_kopecks=None, client_price_kopecks=None)


@pytest.mark.asyncio
async def test_staff_details_snapshots_clear_and_keep_original(db_session):
    db = db_session
    client, admin = await people(db)
    item = await create_repair_request(db, client.id, "Не работает компьютер")
    db.add(assessment(item.id))
    await db.commit()
    await add_request_staff_edit(db, item.id, admin.id, "Проверенный диагноз", 10000, parts="Заменить память", tools="Мультиметр")
    await add_request_staff_edit(db, item.id, admin.id, "Уточнение", 20000, tools="")
    saved = await get_repair_request(db, item.id)
    assert effective_staff_details(saved) == ("Заменить память", "")
    assert saved.staff_edits[0].details.tools == "Мультиметр"
    assert saved.staff_edits[-1].editor_name == admin.full_name
    assert saved.ai_assessment.parts == "Исходные детали"


@pytest.mark.asyncio
async def test_reverse_code_expiration_single_use_and_cooldown(db_session, monkeypatch):
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-secret-not-a-real-key")
    db = db_session
    client, _ = await people(db)
    a = WebsiteAccount(email="otp@test.invalid", password_hash="unused")
    b = WebsiteAccount(email="otp2@test.invalid", password_hash="unused")
    db.add_all([a, b])
    await db.commit()
    status, code = await issue_telegram_code(db, client.telegram_id)
    assert status == "issued" and len(code) == 8
    row = await db.scalar(select(TelegramAccessCode))
    assert row.code_hash == code_digest(code) and code not in row.code_hash
    assert (await issue_telegram_code(db, client.telegram_id))[0] == "cooldown"
    row.expires_at = utcnow() - timedelta(seconds=1)
    await db.commit()
    assert await verify_telegram_code(db, a.id, code, "test") == "invalid"
    row.expires_at = utcnow() + timedelta(minutes=1)
    await db.commit()
    assert await verify_telegram_code(db, a.id, code, "test") == "linked"
    assert await verify_telegram_code(db, b.id, code, "test") == "invalid"
    await db.refresh(a)
    assert a.user_id == client.id
    assert (await issue_telegram_code(db, client.telegram_id))[0] == "already_linked"


@pytest.mark.asyncio
async def test_reverse_code_rate_limit_persists(db_session_factory, monkeypatch):
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-secret-not-a-real-key")
    async with db_session_factory() as db:
        a = WebsiteAccount(email="rate@test.invalid", password_hash="unused")
        db.add(a)
        await db.commit()
        account_id = a.id
    for _ in range(5):
        async with db_session_factory() as db:
            assert await verify_telegram_code(db, account_id, "00000000", "ip") == "invalid"
    async with db_session_factory() as db:
        assert await verify_telegram_code(db, account_id, "00000000", "new-ip") == "rate_limited"


@pytest.mark.asyncio
async def test_code_never_sent_to_group():
    from repair_bot.handlers.registration import send_site_code
    message = SimpleNamespace(chat=SimpleNamespace(type="group", id=-99), answer=AsyncMock())
    await send_site_code(message, 95501)
    assert "личном чате" in message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_wallet_first_free_paid_and_staff_bypass(db_session):
    db = db_session
    client, admin = await people(db)
    one = await create_repair_request(db, client.id, "Первый компьютер")
    two = await create_repair_request(db, client.id, "Второй компьютер")
    assert (await reserve_analysis(db, one.id, client.id))[0] == "free"
    assert (await reserve_analysis(db, one.id, client.id))[0] == "busy"
    assert (await reserve_analysis(db, two.id, client.id))[0] == "payment_required"
    await grant_analysis(db, admin, client, 1)
    kind, attempt = await reserve_analysis(db, two.id, client.id)
    assert kind == "paid"
    await finish_analysis(db, two.id, attempt, "failed")
    assert (await get_wallet(db, client)).credits == 0
    assert (await reserve_analysis(db, two.id, client.id))[0] == "payment_required"
    assert (await reserve_analysis(db, two.id, admin.id))[0] == "staff"
    with pytest.raises(ValueError):
        await grant_analysis(db, client, client, 1)


@pytest.mark.asyncio
async def test_historical_ai_consumes_free_and_missing_provider_refunds(db_session_factory):
    async with db_session_factory() as db:
        client, _ = await people(db)
        old = await create_repair_request(db, client.id, "Старый ремонт")
        db.add(assessment(old.id))
        await db.commit()
        assert (await get_wallet(db, client)).free_used
        newer = await create_repair_request(db, client.id, "Новый ремонт")
        client_id, request_id = client.id, newer.id
    analyzer = AsyncMock(return_value=None)
    assert await analyze_with_budget(db_session_factory, request_id, client_id, analyzer, "Описание") == "payment_required"
    analyzer.assert_not_awaited()
    async with db_session_factory() as db:
        other = await create_user(db, "95503", "Другой клиент", "+79990000003")
        item = await create_repair_request(db, other.id, "Компьютер не включается")
        oid, rid = other.id, item.id
    assert await analyze_with_budget(db_session_factory, rid, oid, analyzer, "Описание") == "unavailable"
    async with db_session_factory() as db:
        assert not (await get_wallet(db, await db.get(User, oid))).free_used


@pytest.mark.asyncio
async def test_concurrent_free_reservation(db_session_factory):
    async with db_session_factory() as db:
        client, _ = await people(db)
        items = [await create_repair_request(db, client.id, "Компьютер не включается") for _ in range(2)]
        ids, uid = [i.id for i in items], client.id
    async def reserve(rid):
        async with db_session_factory() as db:
            return (await reserve_analysis(db, rid, uid))[0]
    results = await asyncio.gather(*(reserve(i) for i in ids))
    assert sorted(results) == ["free", "payment_required"]


@pytest.mark.asyncio
async def test_same_request_only_one_concurrent_attempt(db_session_factory):
    async with db_session_factory() as db:
        client, _ = await people(db)
        item = await create_repair_request(db, client.id, "Не работает компьютер")
        rid, uid = item.id, client.id
    async def reserve():
        async with db_session_factory() as db:
            return (await reserve_analysis(db, rid, uid))[0]
    assert sorted(await asyncio.gather(reserve(), reserve())) == ["busy", "free"]


@pytest.mark.asyncio
async def test_grant_is_idempotent_and_audited(db_session):
    from repair_bot.database.models import AiCreditGrant
    db = db_session
    client, admin = await people(db)
    await grant_analysis(db, admin, client, 2, reference="test-payment")
    await grant_analysis(db, admin, client, 2, reference="test-payment")
    await grant_analysis(db, admin, client, 1, reference="test-support", support=True)
    assert (await get_wallet(db, client)).credits == 3
    grants = (await db.scalars(select(AiCreditGrant).order_by(AiCreditGrant.id))).all()
    assert [(g.amount_kopecks, g.reason) for g in grants] == [(6000, "payment_verified"), (0, "support")]
    assert all(g.actor_name == admin.full_name for g in grants)


@pytest.mark.asyncio
async def test_ai_is_shared_between_bot_and_site_and_photo_survives(db_session_factory, monkeypatch):
    import repair_bot.handlers.client_requests as handlers
    from repair_bot.services.ai.diagnostics import AiResult
    monkeypatch.setattr(handlers, "SessionLocal", db_session_factory)
    analyzer = AsyncMock(return_value=AiResult(priority="medium", client_causes="Причина", client_recommendations="Совет", safety_note="Безопасность", master_diagnosis="Диагноз", parts="Память", tools="Тестер", catalog_item_codes=[]))
    monkeypatch.setattr(handlers, "analyze_repair_request", analyzer)
    async with db_session_factory() as db:
        client, admin = await people(db)
        tg, uid = int(client.telegram_id), client.id
    created, _ = await handlers.create_and_analyze_request(tg, "Проблема с компьютером", "test-photo", b"photo")
    assert created.ai_assessment is not None
    second, _ = await handlers.create_and_analyze_request(tg, "Другая проблема с компьютером", "second-photo", b"photo2")
    assert second.ai_assessment is None
    assert request_photo_id(second) == "second-photo"
    analyzer.assert_awaited_once()
    assert await analyze_with_budget(db_session_factory, second.id, uid, analyzer, "Описание") == "payment_required"
    analyzer.assert_awaited_once()


@pytest.mark.asyncio
async def test_staff_fsm_details_and_role_revocation(db_session_factory, monkeypatch):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.fsm.storage.base import StorageKey
    import repair_bot.handlers.staff_requests as handlers
    from repair_bot.states import RequestStates
    monkeypatch.setattr(handlers, "SessionLocal", db_session_factory)
    monkeypatch.setattr(handlers, "edit_state_screen", AsyncMock())
    async with db_session_factory() as db:
        _, admin = await people(db)
        uid, tid = admin.id, int(admin.telegram_id)
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=tid, user_id=tid))
    await state.set_state(RequestStates.waiting_staff_parts)
    await state.update_data(staff_parts="Память", staff_tools="Тестер")
    message = SimpleNamespace(from_user=SimpleNamespace(id=tid), text="=", answer=AsyncMock())
    await handlers.process_staff_parts(message, state)
    assert (await state.get_data())["staff_parts"] == "Память"
    assert await state.get_state() == RequestStates.waiting_staff_tools.state
    message.text = "-"
    await handlers.process_staff_tools(message, state)
    assert (await state.get_data())["staff_tools"] == ""
    assert await state.get_state() == RequestStates.waiting_staff_price.state
    async with db_session_factory() as db:
        admin = await db.get(User, uid)
        admin.role = "client"
        await db.commit()
    await handlers.process_staff_parts(message, state)
    assert await state.get_state() is None
    message.answer.assert_awaited_once()
    await storage.close()


@pytest.mark.asyncio
async def test_reverse_otp_website_endpoint_keeps_old_flow(db_session_factory, monkeypatch):
    monkeypatch.setattr(web, "SessionLocal", db_session_factory)
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-secret-not-a-real-key")
    async with db_session_factory() as db:
        client, _ = await people(db)
        _, code = await issue_telegram_code(db, client.telegram_id)
        user_id = client.id
    async with AsyncClient(transport=ASGITransport(app=web.app), base_url="http://testserver") as browser:
        token = csrf(await browser.get("/register"))
        await browser.post("/register", data={"csrf": token, "email": "reverse@test.invalid", "password": "password123", "password_repeat": "password123"})
        page = await browser.get("/panel")
        assert "/account/telegram-code" in page.text and "/account/link-code" in page.text
        token = csrf(page)
        assert (await browser.post("/account/telegram-code", data={"csrf": "wrong", "code": code})).status_code == 403
        assert (await browser.post("/account/telegram-code", data={"csrf": token, "code": code})).status_code == 303
        async with db_session_factory() as db:
            account = await db.scalar(select(WebsiteAccount).where(WebsiteAccount.email == "reverse@test.invalid"))
            assert account.user_id == user_id


@pytest.mark.asyncio
async def test_trash_hides_from_owner_and_staff_keeps_photo_and_history(db_session):
    db = db_session
    client, admin = await people(db)
    item = await create_repair_request(db, client.id, "Компьютер не включается")
    await set_request_photo(db, item.id, "test-photo")
    assert not await move_to_trash(db, item.id, client)
    assert await move_to_trash(db, item.id, admin)
    assert await get_repair_request(db, item.id) is None
    assert await get_all_requests(db) == []
    assert await get_user_requests(db, client.id) == []
    trashed = await get_repair_request(db, item.id, include_deleted=True)
    assert request_photo_id(trashed) == "test-photo"
    assert len(await get_all_requests(db, trash=True)) == 1
    await move_to_trash(db, item.id, admin, restore=True)
    assert (await get_repair_request(db, item.id)).description == item.description


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["json", "csv"])
async def test_transfer_preview_atomic_idempotent_no_ai_or_gifts(db_session, kind):
    db = db_session
    client, admin = await people(db)
    item = await create_repair_request(db, client.id, "=Не работает компьютер")
    await add_request_staff_edit(db, item.id, admin.id, "Проверен", 2000000, parts="Память", tools="Тестер")
    item = await get_repair_request(db, item.id)
    row = export_row(item, "Улица Тестовая")
    row["source_id"] = 888
    row["status"] = "completed"
    payload = serialize_rows([row], kind)
    assert parse_transfer(payload, kind)[0] == row
    if kind == "csv":
        assert "'=Не работает" in payload and "'+7999" in payload
    batch, fresh, skipped = await preview_import(db, admin, payload, kind)
    assert len(fresh) == 1 and skipped == 0
    assert await db.scalar(select(func.count(RepairRequest.id))) == 1
    assert await confirm_import(db, admin, batch.id) == 1
    with pytest.raises(ValueError):
        await confirm_import(db, admin, batch.id)
    imported = (await get_all_requests(db))[0]
    # Сортировка по дате может поместить импорт рядом с оригиналом.
    imported = next(i for i in await get_all_requests(db) if i.lifecycle and i.lifecycle.imported)
    assert imported.staff_edits[-1].details.tools == "Тестер"
    assert imported.user.role == "client"
    assert await issue_for_request(db, imported.id) is None
    assert await db.scalar(select(func.count(GiftCertificate.id))) == 0
    assert await db.scalar(select(func.count(AiUsage.request_id))) == 0
    again, fresh, skipped = await preview_import(db, admin, payload, kind)
    assert fresh == [] and skipped == 1
    assert await confirm_import(db, admin, again.id) == 0


@pytest.mark.asyncio
async def test_import_invalid_archive_does_not_write_and_batch_owned(db_session):
    db = db_session
    client, admin = await people(db)
    with pytest.raises(ValueError):
        await preview_import(db, admin, '{"version":1,"requests":[{}]}', "json")
    assert await db.scalar(select(func.count(RequestImportBatch.id))) == 0
    with pytest.raises(ValueError):
        await preview_import(db, client, "{}", "json")
    batch = RequestImportBatch(id="expired", actor_id=admin.id, payload="[]", expires_at=utcnow() - timedelta(seconds=1))
    db.add(batch)
    await db.commit()
    with pytest.raises(ValueError):
        await confirm_import(db, admin, batch.id)


def csrf(page) -> str:
    return re.search(r"name=['\"]csrf['\"] value=['\"]([^'\"]+)", page.text).group(1)


def test_migration_helper_backup_and_rejects_destructive_sql(tmp_path):
    from ops.apply_additive_migration import apply_migration
    path = tmp_path / "example.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE example (id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO example VALUES (1, 'keep')")
    sql = tmp_path / "migration.sql"
    sql.write_text("BEGIN IMMEDIATE;\nCREATE TABLE IF NOT EXISTS added (id INTEGER);\nCOMMIT;\n", encoding="utf-8")
    backup = apply_migration(path, sql, tmp_path / "backups")
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT value FROM example").fetchone()[0] == "keep"
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='added'").fetchall()
    sql.write_text("DELETE FROM example;\n", encoding="utf-8")
    with pytest.raises(ValueError):
        apply_migration(path, sql, tmp_path / "backups")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM example").fetchone()[0] == "keep"


@pytest.mark.asyncio
async def test_admin_can_create_own_request_and_tools_are_protected(db_session_factory, monkeypatch):
    monkeypatch.setattr(web, "SessionLocal", db_session_factory)
    monkeypatch.setattr(web, "analyze_repair_request", AsyncMock(return_value=None))
    async with db_session_factory() as db:
        client, admin = await people(db)
        for user, email in ((client, "client@new.test"), (admin, "admin@new.test")):
            db.add(WebsiteAccount(email=email, password_hash=web.hash_password("password123"), user_id=user.id))
        await db.commit()
        cid, aid = client.id, admin.id
    async with AsyncClient(transport=ASGITransport(app=web.app), base_url="http://testserver") as browser:
        page = await browser.get("/login")
        token = csrf(page)
        await browser.post("/login", data={"csrf": token, "email": "admin@new.test", "password": "password123"})
        form = await browser.get("/panel?tab=requests&view=client&create=1")
        assert "fieldset disabled" not in form.text
        token = csrf(form)
        response = await browser.post("/requests/create", data={"csrf": token, "description": "Компьютер не запускается", "user_id": cid})
        assert response.status_code == 303 and "created=1" in response.headers["location"]
        async with db_session_factory() as db:
            item = (await get_all_requests(db))[0]
            assert item.user_id == aid
            rid = item.id
        page = await browser.get("/admin/requests")
        assert "Корзина" in page.text and "Экспорт JSON" in page.text
        assert (await browser.get("/admin/requests/import")).status_code == 200
        assert (await browser.get("/admin/ai")).status_code == 200
        assert (await browser.post(f"/admin/requests/{rid}/trash", data={"csrf": "wrong"})).status_code == 403
        assert (await browser.post(f"/admin/requests/{rid}/trash", data={"csrf": token})).status_code == 303
        detail = await browser.get(f"/requests/{rid}")
        assert "Восстановить заявку" in detail.text and "Сохранить проверку" not in detail.text
        await browser.post("/logout", data={"csrf": token})
        token = csrf(await browser.get("/login"))
        await browser.post("/login", data={"csrf": token, "email": "client@new.test", "password": "password123"})
        for url in ("/admin/ai", "/admin/requests/export", "/admin/requests/import", f"/requests/{rid}"):
            assert (await browser.get(url)).status_code == 403
        token = csrf(await browser.get("/panel"))
        assert (await browser.post("/admin/ai/grant", data={"csrf": token, "user_id": cid, "count": 99, "reason": "support"})).status_code == 403


@pytest.mark.asyncio
async def test_additive_access_migration_preserves_old_tables(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    new_names = {"ai_wallets", "ai_usages", "ai_credit_grants", "request_lifecycle", "request_import_batches", "telegram_access_codes", "link_code_rate_limits", "staff_edit_details"}
    path = tmp_path / "old-access.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[t for t in Base.metadata.sorted_tables if t.name not in new_names]))
        await conn.execute(User.__table__.insert().values(telegram_id="95510", full_name="Existing", phone="89999999999", role="client", created_at=datetime(2026, 11, 1)))
    await engine.dispose()
    with sqlite3.connect(path) as db:
        before = db.execute("select * from users").fetchall()
        columns = db.execute("pragma table_info(repair_requests)").fetchall()
        sql = Path("ops/migrations/20261109_access_and_transfer.sql").read_text(encoding="utf-8")
        db.executescript(sql)
        db.executescript(sql)
        assert db.execute("select * from users").fetchall() == before
        assert db.execute("pragma table_info(repair_requests)").fetchall() == columns
        assert db.execute("pragma integrity_check").fetchone()[0] == "ok"
