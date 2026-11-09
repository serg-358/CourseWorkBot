"""Фото сайта, точный AI-счётчик и защита закреплённого администратора."""

import re
import secrets
import sqlite3
import tarfile
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import func, select

import repair_bot.web.app as web
import repair_bot.services.request_photos as photos
from repair_bot.database.crud.ai_access import available_analyses, get_wallet, set_analysis_balance, reserve_analysis, finish_analysis
from repair_bot.database.crud.requests import create_repair_request, get_all_requests
from repair_bot.database.crud.users import create_user
from repair_bot.database.models import AiCreditGrant, AiWallet, RepairRequest, WebsiteAccount


def picture() -> bytes:
    output = BytesIO()
    Image.new("RGB", (64, 48), "blue").save(output, "PNG")
    return output.getvalue()


def csrf(page) -> str:
    return re.search(r"name=['\"]csrf['\"] value=['\"]([^'\"]+)", page.text).group(1)


async def login(browser: AsyncClient, name: str) -> str:
    token = csrf(await browser.get("/login"))
    result = await browser.post("/login", data={"csrf": token, "email": name + "@photo.test", "password": "password123"})
    assert result.status_code == 303
    return csrf(await browser.get("/panel"))


@pytest_asyncio.fixture
async def site(db_session_factory, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "SessionLocal", db_session_factory)
    monkeypatch.setattr(web, "analyze_repair_request", AsyncMock(return_value=None))
    monkeypatch.setattr(photos, "PHOTO_DIRECTORY", tmp_path / "private_photos")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "96601")
    users = {}
    async with db_session_factory() as db:
        for number, name, role in ((96601, "owner", "admin"), (96602, "other", "admin"), (96603, "client", "client")):
            user = await create_user(db, str(number), name.title(), "+79990000001", role)
            users[name] = user.id
            db.add(WebsiteAccount(email=name + "@photo.test", password_hash=web.hash_password("password123"), user_id=user.id))
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=web.app), base_url="http://testserver") as browser:
        yield browser, users


@pytest.mark.asyncio
async def test_photo_create_without_ai_quota_and_protected_view(site, db_session_factory):
    browser, users = site
    async with db_session_factory() as db:
        db.add(AiWallet(telegram_id="96603", free_used=True, credits=0))
        await db.commit()
    token = await login(browser, "client")
    form = await browser.get("/panel?tab=requests&create=1")
    assert "multipart/form-data" in form.text and "name='photo'" in form.text
    response = await browser.post("/requests/create", data={"csrf": token, "description": "Компьютер не включается"}, files={"photo": ("../../picture.exe", picture(), "image/png")})
    assert response.status_code == 303
    web.analyze_repair_request.assert_not_awaited()
    async with db_session_factory() as db:
        item = (await get_all_requests(db))[0]
        assert item.user_id == users["client"] and item.ai_assessment is None
        assert re.fullmatch(r"web:[a-f0-9]{32}\.jpg", item.lifecycle.photo_file_id)
        path = photos.photo_path(item.lifecycle.photo_file_id)
        assert path.is_file() and path.parent == photos.PHOTO_DIRECTORY
        rid = item.id
    photo = await browser.get(f"/requests/{rid}/photo")
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
    assert photo.headers["cache-control"] == "private, no-store"
    with Image.open(BytesIO(photo.content)) as image:
        assert image.format == "JPEG" and not image.getexif()
    await browser.post("/logout", data={"csrf": token})
    assert (await browser.get(f"/requests/{rid}/photo")).status_code == 403


@pytest.mark.asyncio
async def test_site_photo_is_passed_to_ai_and_other_client_cannot_attach(site, db_session_factory):
    browser, users = site
    token = await login(browser, "owner")
    response = await browser.post("/requests/create", data={"csrf": token, "description": "Проблема с разъёмом компьютера"}, files={"photo": ("photo.png", picture(), "image/png")})
    assert response.status_code == 303
    call = web.analyze_repair_request.call_args
    assert call.args[1].startswith(b"\xff\xd8")
    async with db_session_factory() as db:
        item = (await get_all_requests(db))[0]
        rid = item.id
    await browser.post("/logout", data={"csrf": token})
    token = await login(browser, "client")
    assert (await browser.get(f"/requests/{rid}/photo")).status_code == 403
    assert (await browser.post(f"/requests/{rid}/photo", data={"csrf": token}, files={"photo": ("photo.png", picture(), "image/png")})).status_code == 403


@pytest.mark.asyncio
async def test_attach_photo_to_existing_request_does_not_run_ai(site, db_session_factory):
    browser, users = site
    async with db_session_factory() as db:
        item = await create_repair_request(db, users["client"], "Компьютер не работает")
        rid = item.id
    token = await login(browser, "client")
    page = await browser.get(f"/requests/{rid}")
    assert "Добавить фотографию" in page.text
    response = await browser.post(f"/requests/{rid}/photo", data={"csrf": token}, files={"photo": ("photo.png", picture(), "image/png")})
    assert response.status_code == 303
    assert (await browser.get(f"/requests/{rid}/photo")).status_code == 200
    web.analyze_repair_request.assert_not_awaited()
    assert (await browser.post(f"/requests/{rid}/photo", data={"csrf": token}, files={"photo": ("photo.png", picture(), "image/png")})).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,token_valid", [(b"<svg>not a photo</svg>", True), (b"X" * (photos.MAX_PHOTO_BYTES + 1), True), (picture(), False)], ids=["invalid-image", "too-large", "bad-csrf"])
async def test_bad_upload_or_csrf_does_not_create_request(site, db_session_factory, payload, token_valid):
    browser, _ = site
    token = await login(browser, "client")
    response = await browser.post("/requests/create", data={"csrf": token if token_valid else "wrong", "description": "Не работает компьютер"}, files={"photo": ("photo.png", payload, "image/png")})
    assert response.status_code == 200
    assert "Не удалось выполнить действие" in response.text
    async with db_session_factory() as db:
        assert await db.scalar(select(func.count(RepairRequest.id))) == 0
    assert not photos.PHOTO_DIRECTORY.exists()
    web.analyze_repair_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_counter_ui_for_only_admins_and_exact_set(site, db_session_factory):
    browser, users = site
    token = await login(browser, "owner")
    page = await browser.get("/admin/ai")
    assert page.text.count('action="/admin/ai/set"') == 3
    assert "Безлимит" in page.text or "без лимита" in page.text
    page = await browser.get("/admin/users")
    assert "AI-попытки" in page.text and "роль закреплена на сервере" in page.text
    assert f"action='/admin/users/{users['owner']}/role'" not in page.text
    reference = secrets.token_urlsafe(24)
    data = {"csrf": token, "user_id": users["client"], "count": "5", "reference": reference}
    assert (await browser.post("/admin/ai/set", data=data)).status_code == 303
    async with db_session_factory() as db:
        wallet = await db.get(AiWallet, "96603")
        assert available_analyses(wallet) == 5 and wallet.free_used
    # Повтор POST не меняет остаток, даже если попытки уже использовались.
    async with db_session_factory() as db:
        wallet = await db.get(AiWallet, "96603")
        wallet.credits = 4
        await db.commit()
    await browser.post("/admin/ai/set", data=data)
    async with db_session_factory() as db:
        assert (await db.get(AiWallet, "96603")).credits == 4
        grant = await db.scalar(select(AiCreditGrant).where(AiCreditGrant.reference == reference))
        assert grant.credits == 5 and grant.reason == "balance_set" and grant.actor_name == "Owner"
    data.update(count="0", reference=secrets.token_urlsafe(24))
    await browser.post("/admin/ai/set", data=data)
    async with db_session_factory() as db:
        assert available_analyses(await db.get(AiWallet, "96603")) == 0


@pytest.mark.asyncio
async def test_counter_permissions_and_protected_owner_cannot_be_demoted(site, db_session_factory):
    browser, users = site
    token = await login(browser, "other")
    result = await browser.post(f"/admin/users/{users['owner']}/role", data={"csrf": token, "role": "client"})
    assert result.status_code == 200
    assert "закреплена" in result.text
    async with db_session_factory() as db:
        from repair_bot.database.models import User
        assert (await db.get(User, users["owner"])).role == "admin"
    await browser.post("/logout", data={"csrf": token})
    token = await login(browser, "client")
    assert (await browser.post("/admin/ai/set", data={"csrf": token, "user_id": users["client"], "count": "999", "reference": secrets.token_urlsafe(24)})).status_code == 403


@pytest.mark.asyncio
async def test_counter_decreases_and_reset_does_not_get_old_refund(db_session):
    from repair_bot.database.models import User
    db = db_session
    client = await create_user(db, "96610", "Client", "+79990000001")
    admin = await create_user(db, "96611", "Admin", "+79990000002", "admin")
    item = await create_repair_request(db, client.id, "Не работает компьютер")
    kind, attempt = await reserve_analysis(db, item.id, client.id)
    assert kind == "free"
    await set_analysis_balance(db, admin, client, 5, "counter-reset-test")
    await finish_analysis(db, item.id, attempt, "unavailable")
    assert available_analyses(await get_wallet(db, client)) == 5
    other = await create_repair_request(db, client.id, "Другой компьютер не работает")
    assert (await reserve_analysis(db, other.id, client.id))[0] == "paid"
    assert available_analyses(await get_wallet(db, client)) == 4
    assert isinstance(client, User)


def test_photo_path_rejects_traversal_and_backup_contains_images(tmp_path):
    from ops.backup_storage import backup_storage
    with pytest.raises(ValueError):
        photos.photo_path("web:../../secret.env")
    database = tmp_path / "bot_database.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE keep (id INTEGER)")
        db.execute("INSERT INTO keep VALUES (1)")
    directory = tmp_path / "request_photos"
    directory.mkdir()
    filename = "a" * 32 + ".jpg"
    (directory / filename).write_bytes(picture())
    (directory / "ignore.txt").write_text("not a photo", encoding="utf-8")
    for _ in range(4):
        archive = backup_storage(database, tmp_path / "backups")
    assert len(list((tmp_path / "backups").glob("storage-*.tar.gz"))) == 3
    with tarfile.open(archive) as saved:
        assert set(saved.getnames()) == {"bot_database.db", "request_photos/" + filename}
        assert saved.extractfile("request_photos/" + filename).read() == picture()
        restored = tmp_path / "restored.db"
        restored.write_bytes(saved.extractfile("bot_database.db").read())
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT id FROM keep").fetchone() == (1,)
