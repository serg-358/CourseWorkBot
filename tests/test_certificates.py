from datetime import datetime
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import create_async_engine

from repair_bot.database.crud.certificates import list_certificates, reserve_certificate, sync_certificates
from repair_bot.database.crud.requests import add_request_staff_edit, create_repair_request, get_repair_request, update_request_status
from repair_bot.database.crud.users import create_user
from repair_bot.database.models import Base, GiftCertificate, User
from repair_bot.handlers.common import client_request_text
from repair_bot.web.workspace import request_price


@pytest.mark.asyncio
async def test_certificate_issue_reserve_finalize_and_release(db_session):
    owner = await create_user(db_session, '94001', 'Certificate owner', '+79990000001')
    other = await create_user(db_session, '94002', 'Another owner', '+79990000002')
    staff = await create_user(db_session, '94003', 'Master', '+79990000003', role='master')
    source = await create_repair_request(db_session, owner.id, 'Source repair', 'urgent')
    await add_request_staff_edit(db_session, source.id, staff.id, 'Confirmed diagnosis', 1_450_000)
    assert await list_certificates(db_session, owner.id) == []
    await update_request_status(db_session, source.id, 'completed')
    gifts = await list_certificates(db_session, owner.id)
    assert len(gifts) == 1
    gift = gifts[0]
    assert gift.amount_kopecks == 100_000
    assert gift.qualifying_total_kopecks == 1_500_000
    await sync_certificates(db_session, owner.id)
    assert len(await list_certificates(db_session, owner.id)) == 1
    assert await list_certificates(db_session, other.id) == []
    target = await create_repair_request(db_session, owner.id, 'Next repair')
    assert not await reserve_certificate(db_session, owner.id, gift.id, target.id)
    await add_request_staff_edit(db_session, target.id, staff.id, 'Confirmed next diagnosis', 200_000)
    assert not await reserve_certificate(db_session, other.id, gift.id, target.id)
    assert await reserve_certificate(db_session, owner.id, gift.id, target.id)
    assert not await reserve_certificate(db_session, owner.id, gift.id, target.id)
    target = await get_repair_request(db_session, target.id)
    assert request_price(target) == 100_000
    assert 'Скидка по сертификату: 1 000 ₽' in client_request_text(target)
    await update_request_status(db_session, target.id, 'cancelled')
    await db_session.refresh(gift)
    assert gift.target_request_id is None and gift.used_at is None
    next_target = await create_repair_request(db_session, owner.id, 'Small next repair')
    await add_request_staff_edit(db_session, next_target.id, staff.id, 'Confirmed small repair', 50_000)
    assert await reserve_certificate(db_session, owner.id, gift.id, next_target.id)
    next_target = await get_repair_request(db_session, next_target.id)
    assert request_price(next_target) == 0
    await update_request_status(db_session, next_target.id, 'completed')
    await db_session.refresh(gift)
    assert gift.used_at is not None
    assert not await reserve_certificate(db_session, owner.id, gift.id, target.id)


@pytest.mark.asyncio
async def test_certificate_threshold_and_single_certificate_per_target(db_session):
    owner = await create_user(db_session, '94101', 'Owner', '+79990000001')
    staff = await create_user(db_session, '94102', 'Staff', '+79990000002', role='master')
    source = await create_repair_request(db_session, owner.id, 'Below threshold')
    await add_request_staff_edit(db_session, source.id, staff.id, 'Diagnosis', 1_499_999)
    await update_request_status(db_session, source.id, 'completed')
    assert await list_certificates(db_session, owner.id) == []
    await add_request_staff_edit(db_session, source.id, staff.id, 'Updated diagnosis', 1_500_000)
    second = await create_repair_request(db_session, owner.id, 'Second eligible repair')
    await add_request_staff_edit(db_session, second.id, staff.id, 'Diagnosis', 1_600_000)
    await update_request_status(db_session, second.id, 'completed')
    gifts = await list_certificates(db_session, owner.id)
    assert len(gifts) == 2
    target = await create_repair_request(db_session, owner.id, 'Target repair')
    await add_request_staff_edit(db_session, target.id, staff.id, 'Diagnosis', 1_550_000)
    assert await reserve_certificate(db_session, owner.id, gifts[0].id, target.id)
    assert not await reserve_certificate(db_session, owner.id, gifts[1].id, target.id)
    await update_request_status(db_session, target.id, 'completed')
    # 15 500 - 1 000 = 14 500: новый сертификат не выдаётся.
    assert len(await list_certificates(db_session, owner.id)) == 2


@pytest.mark.asyncio
async def test_additive_certificate_schema_keeps_existing_data(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    try:
        async with engine.begin() as connection:
            old_tables = [table for table in Base.metadata.sorted_tables if table.name != 'gift_certificates']
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=old_tables))
            await connection.execute(User.__table__.insert().values(telegram_id='94501', full_name='Existing user', phone='+79990000001', role='client', created_at=datetime(2026, 11, 1)))
            before = await connection.run_sync(lambda sync: [col['name'] for col in inspect(sync).get_columns('repair_requests')])
        migration = Path('ops/migrations/20261108_gift_certificates.sql').read_text(encoding='utf-8')
        with sqlite3.connect(tmp_path / 'old.db') as db:
            db.executescript(migration)
            db.executescript(migration)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            after = await connection.run_sync(lambda sync: [col['name'] for col in inspect(sync).get_columns('repair_requests')])
            assert before == after
            assert (await connection.execute(select(User.full_name))).scalar_one() == 'Existing user'
            assert (await connection.execute(select(GiftCertificate))).all() == []
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_certificate_web_endpoints_enforce_owner_and_staff_roles(db_session_factory, monkeypatch):
    import re
    from httpx import ASGITransport, AsyncClient
    import repair_bot.web.app as web_app
    from repair_bot.database.models import WebsiteAccount
    monkeypatch.setattr(web_app,'SessionLocal',db_session_factory)
    async with db_session_factory() as db:
        owner=await create_user(db,'94701','Owner','+79990000001')
        other=await create_user(db,'94702','Other','+79990000002')
        staff=await create_user(db,'94703','Admin','+79990000003',role='admin')
        for user,email in [(owner,'gift-owner@example.test'),(other,'gift-other@example.test')]:
            db.add(WebsiteAccount(user_id=user.id,email=email,password_hash=web_app.hash_password('password123')))
        source=await create_repair_request(db,owner.id,'Eligible source')
        await add_request_staff_edit(db,source.id,staff.id,'Diagnosis',1_500_000)
        await update_request_status(db,source.id,'completed')
        gift=(await list_certificates(db,owner.id))[0]
        target=await create_repair_request(db,owner.id,'Eligible next repair')
        await add_request_staff_edit(db,target.id,staff.id,'Diagnosis',200_000)
        gift_id,target_id,code=gift.id,target.id,gift.code
    async with AsyncClient(transport=ASGITransport(app=web_app.app),base_url='http://testserver') as client:
        assert (await client.get('/admin/certificates')).status_code == 403
        for email in ['gift-other@example.test','gift-owner@example.test']:
            page=await client.get('/login')
            csrf=re.search(r"name='csrf' value='([^']+)'",page.text).group(1)
            await client.post('/login',data={'csrf':csrf,'email':email,'password':'password123'})
            page=await client.get('/panel?tab=certificates')
            csrf=re.search(r"name='csrf' value='([^']+)'",page.text).group(1)
            assert (code in page.text) == email.startswith('gift-owner')
            applied=await client.post(f'/certificates/{gift_id}/apply',data={'csrf':csrf,'request_id':str(target_id)},follow_redirects=False)
            if email.startswith('gift-other'):
                assert 'Сертификат недоступен' in applied.text
            else:
                assert applied.status_code == 303
                assert applied.headers['location'] == f'/requests/{target_id}'
                async with db_session_factory() as db:
                    assert request_price(await get_repair_request(db,target_id)) == 100_000
            assert (await client.get('/admin/certificates')).status_code == 403
            await client.post('/logout',data={'csrf':csrf})
