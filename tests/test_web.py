import hashlib
import re
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import repair_bot.web.app as web_app
from repair_bot.database.crud.users import create_user
from repair_bot.database.crud.web import link_telegram_account
from repair_bot.database.models import (
    RequestAddress,
    RequestUrgency,
    RepairRequest,
    User,
    WebsiteAccount,
)


@pytest.mark.asyncio
async def test_website_register_link_telegram_and_enforce_role_access(
    db_session_factory, monkeypatch
):
    monkeypatch.setattr(web_app, "SessionLocal", db_session_factory)
    monkeypatch.setattr(
        web_app, "analyze_repair_request", AsyncMock(return_value=None)
    )
    transport = ASGITransport(app=web_app.app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        register_page = await client.get("/register")
        csrf = re.search(r"name='csrf' value='([^']+)'", register_page.text).group(1)
        response = await client.post(
            "/register",
            data={
                "csrf": csrf,
                "email": "client@example.test",
                "password": "strong-password-123",
                "password_repeat": "strong-password-123",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        # Вход сохраняется при переходе на главную и страницы авторизации.
        signed_in_home = await client.get("/")
        assert "client@example.test" in signed_in_home.text
        assert 'href="/register"' not in signed_in_home.text
        for auth_path in ("/login", "/register"):
            auth_response = await client.get(auth_path, follow_redirects=False)
            assert auth_response.status_code == 303
            assert auth_response.headers["location"] == "/panel"

        unlinked_page = await client.get("/panel")
        assert "Свяжите аккаунт с Telegram" in unlinked_page.text
        csrf = re.search(r"name='csrf' value='([^']+)'", unlinked_page.text).group(1)
        code_page = await client.post(
            "/account/link-code", data={"csrf": csrf}
        )
        code = re.search(r"<pre>/link ([^<]+)</pre>", code_page.text).group(1)

        async with db_session_factory() as db:
            telegram_user = await create_user(
                db, "90001", "Веб-клиент", "+79990000001"
            )
            result = await link_telegram_account(
                db,
                telegram_user.telegram_id,
                hashlib.sha256(code.encode()).hexdigest(),
            )
            assert result == "linked"
            reused = await link_telegram_account(
                db,
                telegram_user.telegram_id,
                hashlib.sha256(code.encode()).hexdigest(),
            )
            assert reused == "invalid"

        client_page = await client.get("/panel")
        assert "Кабинет клиента" in client_page.text
        assert "dashboard-metrics" in client_page.text
        assert "Начните с первой заявки" in client_page.text
        assert "Как проходит ремонт" in client_page.text
        assert "Создать заявку" in client_page.text
        assert "Телефон: +79990000001" in client_page.text
        assert (await client.get("/admin/users")).status_code == 403
        csrf = re.search(r"name='csrf' value='([^']+)'", client_page.text).group(1)
        created = await client.post(
            "/requests/create",
            data={
                "csrf": csrf,
                "description": "Компьютер выключается во время игры.",
                "urgency": "urgent",
                "address": "Улица Примерная, дом 4",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        async with db_session_factory() as db:
            request_rows = await db.execute(
                select(RepairRequest)
            )
            repair_request = request_rows.scalar_one()
            urgency_rows = await db.execute(
                select(RequestUrgency).where(
                    RequestUrgency.request_id == repair_request.id
                )
            )
            urgency = urgency_rows.scalar_one()
            address_rows = await db.execute(
                select(RequestAddress).where(
                    RequestAddress.request_id == repair_request.id
                )
            )
            address = address_rows.scalar_one()
            assert urgency.surcharge_kopecks == 50_000
            assert address.address == "Улица Примерная, дом 4"
        populated_dashboard = await client.get("/panel")
        assert "Ваша текущая заявка" in populated_dashboard.text
        assert "Компьютер выключается во время игры." in populated_dashboard.text
        assert "Последние заявки" in populated_dashboard.text
        assert "Начните с первой заявки" not in populated_dashboard.text


@pytest.mark.asyncio
async def test_admin_can_assign_roles_but_bootstrap_role_is_protected(
    db_session_factory, monkeypatch
):
    monkeypatch.setattr(web_app, "SessionLocal", db_session_factory)
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "91001")
    async with db_session_factory() as db:
        admin = User(
            telegram_id="91001",
            full_name="Администратор",
            phone="+79990000001",
            role="admin",
        )
        client_user = User(
            telegram_id="91002",
            full_name="Клиент",
            phone="+79990000002",
            role="client",
        )
        db.add_all([admin, client_user])
        await db.flush()
        repair_request = RepairRequest(
            user_id=client_user.id,
            description="Перестал включаться компьютер.",
        )
        db.add(repair_request)
        await db.flush()
        account = WebsiteAccount(
            email="admin@example.test",
            password_hash=web_app.hash_password("another-strong-password"),
            user_id=admin.id,
        )
        client_account = WebsiteAccount(
            email="client-master@example.test",
            password_hash=web_app.hash_password("client-strong-password"),
            user_id=client_user.id,
        )
        db.add_all([account, client_account])
        await db.commit()
        client_user_id = client_user.id
        admin_user_id = admin.id
        repair_request_id = repair_request.id

    transport = ASGITransport(app=web_app.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login_page = await client.get("/login")
        csrf = re.search(r"name='csrf' value='([^']+)'", login_page.text).group(1)
        await client.post(
            "/login",
            data={
                "csrf": csrf,
                "email": "admin@example.test",
                "password": "another-strong-password",
            },
            follow_redirects=False,
        )
        admin_page = await client.get("/admin/users")
        assert admin_page.status_code == 200
        admin_only = await client.get("/admin/users?role=admin")
        assert "+79990000001" in admin_only.text
        assert "+79990000002" not in admin_only.text
        client_only = await client.get("/admin/users?role=client")
        assert "+79990000002" in client_only.text
        assert "+79990000001" not in client_only.text
        role_summary = await client.get("/panel")
        for label in ("Администраторы", "Мастера", "Клиенты"):
            assert label in role_summary.text
        csrf = re.search(r"name='csrf' value='([^']+)'", admin_page.text).group(1)
        protected = await client.post(
            f"/admin/users/{admin_user_id}/role",
            data={"csrf": csrf, "role": "client"},
            follow_redirects=False,
        )
        assert "Начальная роль из доверенного списка" in protected.text
        assigned = await client.post(
            f"/admin/users/{client_user_id}/role",
            data={"csrf": csrf, "role": "master"},
            follow_redirects=False,
        )
        assert assigned.status_code == 303
        async with db_session_factory() as db:
            client_user = await db.get(User, client_user_id)
            assert client_user.role == "master"
        admin_panel = await client.get("/panel")
        logout_csrf = re.search(r"name='csrf' value='([^']+)'", admin_panel.text).group(1)
        await client.post("/logout", data={"csrf": logout_csrf})
        login_page = await client.get("/login")
        csrf = re.search(r"name='csrf' value='([^']+)'", login_page.text).group(1)
        await client.post(
            "/login",
            data={
                "csrf": csrf,
                "email": "client-master@example.test",
                "password": "client-strong-password",
            },
            follow_redirects=False,
        )
        master_page = await client.get("/panel")
        assert "Панель мастера" in master_page.text
        assert "Что требует внимания" in master_page.text
        assert "Состояние очереди" in master_page.text
        assert "<table>" in master_page.text
        master_page = await client.get(f"/requests/{repair_request_id}")
        assert "Подтверждённый диагноз" in master_page.text
        monkeypatch.setattr(
            web_app, "notify_client_status", AsyncMock(return_value=None)
        )
        csrf = re.search(r"name='csrf' value='([^']+)'", master_page.text).group(1)
        update_response = await client.post(
            f"/staff/requests/{repair_request_id}/update",
            data={
                "csrf": csrf,
                "diagnosis": "Заменить блок питания.",
                "price": "3500",
                "status": "in_progress",
            },
            follow_redirects=False,
        )
        assert update_response.status_code == 303
        web_app.notify_client_status.assert_awaited_once_with(
            "91002", repair_request_id, "in_progress"
        )


def test_postgres_url_is_normalized_to_async_driver():
    from repair_bot.scripts.migrate_sqlite_to_postgres import normalize_postgres_url

    assert normalize_postgres_url("postgres://u:p@host/db") == (
        "postgresql+asyncpg://u:p@host/db"
    )
    assert normalize_postgres_url("postgresql://u:p@host/db") == (
        "postgresql+asyncpg://u:p@host/db"
    )


@pytest.mark.asyncio
async def test_website_registration_rejects_csrf_failure(db_session_factory, monkeypatch):
    monkeypatch.setattr(web_app, "SessionLocal", db_session_factory)
    transport = ASGITransport(app=web_app.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/register",
            data={
                "email": "attacker@example.test",
                "password": "strong-password-123",
                "password_repeat": "strong-password-123",
            },
        )
        assert "Сессия формы устарела" in response.text
        async with db_session_factory() as db:
            assert await web_app.account_by_email(db, "attacker@example.test") is None


def test_password_hash_uses_scrypt_and_checks_correct_password():
    password_hash = web_app.hash_password("strong-password-123")
    assert password_hash.startswith("scrypt$")
    assert web_app.verify_password("strong-password-123", password_hash)
    assert not web_app.verify_password("wrong-password", password_hash)

@pytest.mark.asyncio
async def test_request_details_photos_and_previews_are_authorized(db_session_factory, monkeypatch):
    monkeypatch.setattr(web_app, 'SessionLocal', db_session_factory)
    from repair_bot.database.models import RepairRequestAiAssessment
    async with db_session_factory() as db:
        owner = User(telegram_id='92001', full_name='Owner', phone='+79990000001', role='client')
        admin = User(telegram_id='92002', full_name='Admin', phone='+79990000002', role='admin')
        db.add_all([owner, admin])
        await db.flush()
        item = RepairRequest(user_id=owner.id, description='Photo repair request')
        db.add(item)
        await db.flush()
        db.add(RepairRequestAiAssessment(request_id=item.id, photo_file_id='test-file', client_causes='Client diagnosis', client_recommendations='Advice', safety_note='', master_diagnosis='Private diagnosis', parts='', tools='', catalog_item_codes=''))
        for user, email in [(owner, 'owner@example.test'), (admin, 'admin-photo@example.test')]:
            db.add(WebsiteAccount(user_id=user.id, email=email, password_hash=web_app.hash_password('test-pass-123')))
        await db.commit()
        request_id = item.id
    mocked_photo = AsyncMock(return_value=b'jpeg-content')
    monkeypatch.setattr(web_app, 'download_request_photo', mocked_photo)
    transport = ASGITransport(app=web_app.app)
    async with AsyncClient(transport=transport, base_url='http://testserver') as client:
        assert (await client.get(f'/requests/{request_id}/photo')).status_code == 403
        mocked_photo.assert_not_awaited()
        for email in ['admin-photo@example.test', 'owner@example.test']:
            page = await client.get('/login')
            csrf = re.search(r"name='csrf' value='([^']+)'", page.text).group(1)
            await client.post('/login', data={'csrf':csrf, 'email':email, 'password':'test-pass-123'})
            if email.startswith('admin'):
                preview = await client.get('/panel?view=client')
                assert 'Кабинет клиента' in preview.text
                assert 'Photo repair request' not in preview.text
                assert (await client.get(f'/requests/{request_id}?view=client')).status_code == 403
                assert (await client.get(f'/requests/{request_id}/photo?view=client')).status_code == 403
                master = await client.get('/panel?view=master')
                assert 'Photo repair request' in master.text
                async with db_session_factory() as db:
                    assert (await db.get(User, admin.id)).role == 'admin'
            else:
                detail = await client.get(f'/requests/{request_id}')
                assert f'/requests/{request_id}/photo' in detail.text
                assert 'Private diagnosis' not in detail.text
                photo = await client.get(f'/requests/{request_id}/photo')
                assert photo.content == b'jpeg-content'
                assert photo.headers['cache-control'] == 'private, no-store'
                assert (await client.get(f'/requests/{request_id + 100}/photo')).status_code == 403
                assert 'Посмотреть интерфейс:' not in (await client.get('/panel?view=master')).text
            page = await client.get('/panel')
            csrf = re.search(r"name='csrf' value='([^']+)'", page.text).group(1)
            await client.post('/logout', data={'csrf':csrf})
        qr = await client.get('/bot-qr.png')
        assert qr.status_code == 200
        assert qr.content.startswith(b'\x89PNG')

@pytest.mark.parametrize('field,order,expected', [('id','asc',[1,2,3]),('id','desc',[3,2,1]),('date','asc',[3,1,2]),('status','asc',[2,1,3]),('price','asc',[2,1,3]),('price','desc',[1,2,3])])
def test_workspace_sorts_prices_dates_ids_and_statuses(field, order, expected):
    from datetime import datetime
    from types import SimpleNamespace as Item
    from repair_bot.web.workspace import sorted_requests, request_price
    def item(number, day, status, price, edits=None):
        return Item(id=number, created_at=datetime(2026,11,day), status=status, staff_edits=edits or [], ai_assessment=Item(client_price_kopecks=price), urgency=Item(surcharge_kopecks=50000))
    items = [item(1,2,'in_progress',100000,[Item(client_price_kopecks=300000)]),item(2,3,'new',100000),item(3,1,'completed',None)]
    assert request_price(items[0]) == 350000
    assert [row.id for row in sorted_requests(items, {'sort':field,'order':order})] == expected
    assert [row.id for row in sorted_requests(items, {'status':'new'})] == [2]
    assert [row.id for row in sorted_requests(items, {'from':'2026-11-02','to':'2026-11-02'})] == [1]
    assert [row.id for row in sorted_requests(items, {'status':'closed'})] == [3]
    assert [row.id for row in sorted_requests(items, {'status':'active'})] == [2,1]


@pytest.mark.asyncio
async def test_workspace_tabs_and_feedback_delivery(db_session_factory, monkeypatch):
    monkeypatch.setattr(web_app,'SessionLocal',db_session_factory)
    delivery = AsyncMock(return_value=True)
    monkeypatch.setattr(web_app,'deliver_feedback',delivery)
    async with db_session_factory() as db:
        user = User(telegram_id='93001', full_name='Client workspace', phone='+79990000001', role='client')
        admin = User(telegram_id='93002', full_name='Admin workspace', phone='+79990000002', role='admin')
        db.add_all([user,admin])
        await db.flush()
        db.add(WebsiteAccount(user_id=user.id,email='workspace@example.test',password_hash=web_app.hash_password('test-pass-123')))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=web_app.app),base_url='http://testserver') as client:
        page=await client.get('/login')
        csrf=re.search(r"name='csrf' value='([^']+)'",page.text).group(1)
        await client.post('/login',data={'csrf':csrf,'email':'workspace@example.test','password':'test-pass-123'})
        overview=await client.get('/panel?tab=overview')
        assert 'Здравствуйте, Client workspace!' in overview.text
        assert 'На сайт' not in overview.text
        assert 'Обратная связь' in overview.text
        assert 'workspace@example.test' in (await client.get('/panel?tab=profile')).text
        assert 'Тарифы срочности' in (await client.get('/panel?tab=tariffs')).text
        page=await client.get('/panel?tab=feedback')
        csrf=re.search(r"name='csrf' value='([^']+)'",page.text).group(1)
        await client.post('/feedback',data={'message':'Message without csrf'})
        delivery.assert_not_awaited()
        delivered=await client.post('/feedback',data={'csrf':csrf,'message':'Please help with my repair request.'})
        assert 'Сообщение отправлено' in delivered.text
        assert delivery.await_args.args[0] == ['93002']
        assert 'workspace@example.test' in delivery.await_args.args[1]
        repeated=await client.post('/feedback',data={'csrf':csrf,'message':'Please help with my repair request.'})
        assert 'Подождите минуту' in repeated.text
        assert delivery.await_count == 1

@pytest.mark.parametrize('order,expected', [('desc',[3,1,2]),('asc',[2,1,3])])
def test_priority_sort_and_combined_filter(order, expected):
    from datetime import datetime
    from types import SimpleNamespace as Item
    from repair_bot.web.workspace import sorted_requests, requests_table
    items = [Item(id=number, priority=priority, status=status, created_at=datetime(2026,11,8), description='Repair request', staff_edits=[], ai_assessment=None, urgency=None) for number,priority,status in [(1,'medium','new'),(2,'low','new'),(3,'high','in_progress')]]
    assert [item.id for item in sorted_requests(items, {'sort':'priority','order':order})] == expected
    assert [item.id for item in sorted_requests(items, {'priority':'high','status':'in_progress','from':'2026-11-08','to':'2026-11-08'})] == [3]
    assert sorted_requests(items, {'priority':'high','status':'new'}) == []
    page=requests_table(items,params={'priority':'high','sort':'priority','order':order})
    assert "name='priority'" in page
    assert 'По приоритету' in page
    assert 'priority=high' in page
    assert '№3' in page and '№1' not in page

@pytest.mark.asyncio
async def test_onsite_request_entry_prices_and_clean_request_list(db_session_factory, monkeypatch):
    monkeypatch.setattr(web_app, 'SessionLocal', db_session_factory)
    async with AsyncClient(transport=ASGITransport(app=web_app.app),base_url='http://testserver') as client:
        home=await client.get('/')
        assert 'href="/request/new"' in home.text
        assert 'Демонстрационный пример' not in home.text
        services=await client.get('/services')
        for title in ['Диагностика','Чистка и обслуживание охлаждения','Переустановка ОС','Комплектующие','Тарифы срочности']:
            assert title in services.text
        assert '575 ₽' in services.text
        warranty=await client.get('/documents')
        assert '30 ' in warranty.text
        assert 'Документы о квалификации пока не опубликованы' in warranty.text
        start=await client.get('/request/new',follow_redirects=False)
        assert start.headers['location'] == '/login'
        register=await client.get('/register')
        csrf=re.search(r"name='csrf' value='([^']+)'",register.text).group(1)
        created=await client.post('/register',data={'csrf':csrf,'email':'onsite@example.test','password':'password123','password_repeat':'password123'},follow_redirects=False)
        assert created.headers['location'] == '/request/new'
        onboarding=await client.get('/request/new')
        assert 'Свяжите аккаунт с Telegram' in onboarding.text
        async with db_session_factory() as db:
            user=await create_user(db,'94601','Onsite client','+79990000001')
            account=await web_app.account_by_email(db,'onsite@example.test')
            account.user_id=user.id
            await db.commit()
        requests=await client.get('/panel?tab=requests&create=1',follow_redirects=True)
        assert '<h1>Мои заявки</h1>' in requests.text
        assert '<h1>Кабинет клиента</h1>' not in requests.text
        assert 'Здравствуйте,' not in requests.text
        assert "create-request-panel' open" in requests.text
        assert (await client.post('/certificates/sync')).status_code == 403
        assert (await client.post('/certificates/1/apply',data={'request_id':'1'})).status_code == 403
        certificates=await client.get('/panel?tab=certificates')
        assert 'Сертификатов пока нет' in certificates.text
