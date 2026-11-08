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
