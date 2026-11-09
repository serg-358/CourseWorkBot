"""Веб-кабинет с отдельным доступом для каждой роли."""

from io import BytesIO

import qrcode
import hashlib
import asyncio
import html
import hmac
import logging
import os
import re
import secrets
import time
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

from repair_bot.database.crud.pricing import (
    get_urgency_tariffs,
    get_urgency_tariff,
    update_urgency_tariff,
)
from repair_bot.database.crud.requests import (
    add_request_staff_edit,
    create_repair_request,
    get_all_requests,
    get_repair_request,
    get_user_requests,
    update_request_status,
)
from repair_bot.database.crud.telegram_codes import verify_telegram_code
from repair_bot.services.staff_assessment import effective_staff_details
from repair_bot.database.crud.users import get_all_users
from repair_bot.database.crud.certificates import list_certificates, reserve_certificate, sync_certificates
from repair_bot.database.models import (
    GiftCertificate,
    RepairRequest,
    RequestAddress,
    SiteText,
    User,
    WebsiteAccount,
)
from repair_bot.database.session import SessionLocal, create_tables
from repair_bot.handlers.common import (
    admin_request_text,
    client_request_text,
    configured_role_for_telegram_id,
    format_rubles,
)
from repair_bot.handlers.staff_requests import parse_client_price
from repair_bot.services.ai.diagnostics import analyze_repair_request
from repair_bot.services.ai.budget import analyze_with_budget
from repair_bot.database.crud.request_lifecycle import request_photo_id
from repair_bot.database.crud.ai_access import get_wallet, available_analyses
from repair_bot.services.request_photos import read_photo_form, normalize_photo, save_web_photo, read_web_photo, discard_web_photo
from repair_bot.database.crud.request_lifecycle import set_request_photo
from repair_bot.web.admin_tools import register_admin_tools, transfer_controls, request_tabs, trash_control

from repair_bot.web.workspace import requests_table, profile_content, feedback_content, request_price, STATUSES, PRIORITIES, service_catalog

logger = logging.getLogger(__name__)

SESSION_SECRET = os.getenv("WEB_SESSION_SECRET", "local-development-secret-change-before-deploy")
COOKIE_SECURE = os.getenv("WEB_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().removeprefix("@")
LINK_TTL = timedelta(minutes=10)
ROLE_NAMES = {"client": "Клиент", "master": "Мастер", "admin": "Администратор"}
SITE_TEXT_DEFAULTS = {
    "client_welcome": "Отслеживайте ремонт и создавайте новые заявки в личном кабинете.",
    "service_note": "Выезд по адресу выполняется по согласованию с мастером.",
}
WEB_DIRECTORY = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=WEB_DIRECTORY / "templates")
templates.env.globals["money"] = format_rubles


def create_app() -> FastAPI:
    """Создает отдельное ASGI-приложение сайта."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if os.getenv("ENVIRONMENT") == "production" and (
            len(SESSION_SECRET) < 32
            or SESSION_SECRET == "local-development-secret-change-before-deploy"
        ):
            raise RuntimeError("Для сайта задайте отдельный WEB_SESSION_SECRET.")
        await create_tables()
        yield

    app = FastAPI(
        title="ПК Доктор — личный кабинет",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=SESSION_SECRET,
        session_cookie="pcdoctor_session",
        max_age=8 * 60 * 60,
        same_site="lax",
        https_only=COOKIE_SECURE,
    )
    app.mount(
        "/static",
        StaticFiles(directory=WEB_DIRECTORY / "static"),
        name="static",
    )

    @app.get("/health", response_class=Response)
    async def health() -> Response:
        return Response("ok", media_type="text/plain")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        async with SessionLocal() as db:
            account, _user = await identity(request, db)
        return render_page(request, "Ремонт компьютеров", public_home(account is not None))

    @app.get("/register", response_class=HTMLResponse)
    async def register_page(request: Request) -> Response:
        async with SessionLocal() as db:
            account, _user = await identity(request, db)
        if account is not None:
            return RedirectResponse("/panel", status_code=303)
        return render_page(request, "Регистрация", auth_form(request, "register"))

    @app.get("/request/new")
    async def new_request_entry(request: Request) -> Response:
        async with SessionLocal() as db:
            account, user = await identity(request, db)
            if account is None:
                request.session["after_auth"] = "request"
                return RedirectResponse("/login", status_code=303)
            if user is None:
                request.session["create_after_link"] = True
                return render_page(request, "Подключение Telegram", await unlinked_panel(request, account, db))
            if user.role == "admin":
                return RedirectResponse("/panel?tab=requests&create=1&view=client", status_code=303)
            if user.role != "client":
                return error_page(request, "Для создания клиентской заявки нужен аккаунт клиента. Очередь ремонтов доступна в кабинете мастера.", "/panel")
        return RedirectResponse("/panel?tab=requests&create=1", status_code=303)

    @app.get("/services")
    async def public_services(request: Request) -> Response:
        async with SessionLocal() as db:
            await identity(request, db)
            tariffs = await get_urgency_tariffs(db)
        return render_page(request, "Услуги и цены", templates.get_template("services.html").render(services=service_catalog(), tariffs=tariffs))

    @app.get("/documents")
    async def documents(request: Request) -> Response:
        async with SessionLocal() as db:
            await identity(request, db)
        return render_page(request, "Гарантия и документы", templates.get_template("documents.html").render())

    @app.post("/register")
    async def register(request: Request) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return error_page(request, "Сессия формы устарела. Обновите страницу.", "/register")
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
            return error_page(request, "Введите корректный адрес электронной почты.", "/register")
        if len(password) < 8:
            return error_page(request, "Пароль должен содержать минимум 8 символов.", "/register")
        if password != form.get("password_repeat", ""):
            return error_page(request, "Введённые пароли не совпадают.", "/register")
        async with SessionLocal() as db:
            if await account_by_email(db, email):
                return error_page(request, "Аккаунт с таким email уже зарегистрирован.", "/login")
            account = WebsiteAccount(email=email, password_hash=hash_password(password))
            db.add(account)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                return error_page(request, "Аккаунт с таким email уже зарегистрирован.", "/login")
            await db.refresh(account)
        destination = "/request/new" if request.session.get("after_auth") == "request" else "/panel"
        request.session.clear()
        request.session["account_id"] = account.id
        return RedirectResponse(destination, status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        async with SessionLocal() as db:
            account, _user = await identity(request, db)
        if account is not None:
            return RedirectResponse("/panel", status_code=303)
        return render_page(request, "Вход", auth_form(request, "login"))

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return error_page(request, "Сессия формы устарела. Обновите страницу.", "/login")
        email = form.get("email", "").strip().lower()
        async with SessionLocal() as db:
            account = await account_by_email(db, email)
        if account is None or not verify_password(form.get("password", ""), account.password_hash):
            return error_page(request, "Не удалось войти. Проверьте email и пароль.", "/login")
        destination = "/request/new" if request.session.get("after_auth") == "request" else "/panel"
        request.session.clear()
        request.session["account_id"] = account.id
        return RedirectResponse(destination, status_code=303)

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return error_page(request, "Сессия формы устарела. Обновите страницу.", "/panel")
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/panel", response_class=HTMLResponse)
    async def panel(request: Request) -> Response:
        async with SessionLocal() as db:
            account, user = await identity(request, db)
            if account is None:
                return RedirectResponse("/login", status_code=303)
            if user is not None and request.session.pop("create_after_link", False) and user.role == "client":
                return RedirectResponse("/panel?tab=requests&create=1", status_code=303)
            tab = request.query_params.get("tab", "overview")
            if tab == "profile":
                content = profile_content(account, user)
                if user:
                    wallet = await get_wallet(db, user)
                    await db.commit()
                    balance = "Безлимит по роли сотрудника" if user.role in {"admin", "master"} else str(available_analyses(wallet))
                    content += f"<section class='card'><h2>AI-диагностика</h2><p>Осталось попыток: {balance}</p></section>"
            elif tab == "certificates" and user is not None:
                gifts = await list_certificates(db, user.id)
                items = await get_user_requests(db, user.id)
                eligible = [item for item in items if item.status in {"new", "in_progress"} and item.staff_edits and not item.certificate_discount and request_price(item) > 0]
                content = templates.get_template("certificates.html").render(gifts=gifts, items=eligible, csrf=csrf_token(request), preview=user.role == "admin" and effective_view(request, user) == "client")
            elif tab == "feedback":
                content = feedback_content(csrf_token(request))
            elif tab == "tariffs":
                tariffs = await get_urgency_tariffs(db)
                content = templates.get_template("services.html").render(services=service_catalog(), tariffs=tariffs)
            elif tab == "overview" and user is not None:
                role = effective_view(request, user)
                items = await get_user_requests(db, user.id) if role == "client" else await get_all_requests(db)
                users = await get_all_users(db) if role == "admin" else []
                content = dashboard_content(request, user, role, items, users)
            elif user is None:
                content = await unlinked_panel(request, account, db)
            elif effective_view(request, user) == "client":
                content = await client_panel(request, account, user, db)
            elif effective_view(request, user) == "master":
                content = await master_panel(request, user, db)
            elif user.role == "admin":
                content = await admin_panel(request, user, db)
            else:
                request.session.clear()
                return RedirectResponse("/login", status_code=303)
        return render_page(request, "Личный кабинет", content)

    @app.post("/account/link-code")
    async def create_link_code(request: Request) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return error_page(request, "Сессия формы устарела. Обновите страницу.", "/panel")
        account_id = request.session.get("account_id")
        if not isinstance(account_id, int):
            return RedirectResponse("/login", status_code=303)
        async with SessionLocal() as db:
            account = await db.get(WebsiteAccount, account_id)
            if account is None:
                request.session.clear()
                return RedirectResponse("/login", status_code=303)
            if account.user_id is not None:
                return error_page(request, "Этот аккаунт уже привязан к Telegram.", "/panel")
            code = secrets.token_urlsafe(9)
            from repair_bot.database.crud.web import save_link_code
            from repair_bot.database.models import utcnow

            await save_link_code(
                db,
                account.id,
                hashlib.sha256(code.encode()).hexdigest(),
                utcnow() + LINK_TTL,
            )
        bot_link = "/bot"
        link_content = (
            "<section class='card'><h2>Привязка Telegram</h2>"
            "<p>Откройте бота, завершите регистрацию, затем отправьте команду:</p>"
            f"<pre>/link {html.escape(code)}</pre>"
            "<p>Код одноразовый и действует 10 минут.</p>"
            + (f"<a class='button' href='{html.escape(bot_link)}'>Открыть Telegram-бота</a>" if bot_link else "")
            + "</section>"
        )
        return render_page(request, "Привязка Telegram", link_content)

    @app.post("/account/telegram-code")
    async def confirm_telegram_code(request: Request) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return forbidden_page(request)
        account_id = request.session.get("account_id")
        if not isinstance(account_id, int):
            return RedirectResponse("/login", status_code=303)
        async with SessionLocal() as db:
            result = await verify_telegram_code(db, account_id, form.get("code", ""), request.client.host if request.client else "unknown")
        if result in {"linked", "account_already_linked"}:
            return RedirectResponse("/panel", status_code=303)
        messages = {
            "invalid": "Код неверный, уже использован или истёк. Получите новый в боте.",
            "invalid_format": "Введите 8 цифр кода из Telegram.",
            "rate_limited": "Лимит попыток исчерпан. Подождите 10 минут.",
            "telegram_already_linked": "Этот Telegram уже связан с другим аккаунтом сайта.",
        }
        return error_page(request, messages[result], "/panel")

    @app.post("/requests/create")
    async def create_request_from_site(request: Request) -> Response:
        async with SessionLocal() as db:
            account, user = await identity(request, db)
            if account is None or user is None or user.role not in {"client", "admin"}:
                return forbidden_page(request)
            try:
                form, photo = await read_photo_form(request)
            except ValueError as exc:
                return error_page(request, str(exc), "/request/new")
            if not verify_csrf(request, form):
                return error_page(request, "Сессия формы устарела. Обновите страницу.", "/panel")
            description = form.get("description", "").strip()
            address = form.get("address", "").strip()
            if not 10 <= len(description) <= 4000:
                return error_page(request, "Опишите проблему минимум 10 символами.", "/panel")
            if len(address) > 500:
                return error_page(request, "Адрес не должен превышать 500 символов.", "/panel")
            tariff = await get_urgency_tariff(db, form.get("urgency", "ordinary"))
            if tariff is None:
                return error_page(request, "Выбранный тариф недоступен.", "/panel")
            photo_ref = None
            if photo is not None:
                try:
                    photo = await asyncio.to_thread(normalize_photo, photo)
                except ValueError as exc:
                    return error_page(request, str(exc), "/request/new")
                photo_ref = await save_web_photo(photo)
            try:
                repair_request = await create_repair_request(db, user.id, description, tariff.code, photo_file_id=photo_ref)
            except Exception:
                if photo_ref:
                    await discard_web_photo(photo_ref)
                raise
            if address:
                db.add(RequestAddress(request_id=repair_request.id, address=address))
                account.address = address
                await db.commit()
            actor_id = user.id
        await analyze_with_budget(SessionLocal, repair_request.id, actor_id, analyze_repair_request, description, photo, photo_ref)
        return RedirectResponse(f"/requests/{repair_request.id}?created=1", status_code=303)

    @app.post("/staff/requests/{request_id}/update")
    async def staff_update_request(request: Request, request_id: int) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return error_page(request, "Сессия формы устарела. Обновите страницу.", "/panel")
        async with SessionLocal() as db:
            account, user = await identity(request, db)
            if account is None or user is None or user.role not in {"master", "admin"}:
                return forbidden_page(request)
            repair_request = await get_repair_request(db, request_id)
            if repair_request is None:
                return error_page(request, "Заявка не найдена.", "/panel")
            previous_status = repair_request.status
            owner_telegram_id = repair_request.user.telegram_id
            diagnosis = form.get("diagnosis", "").strip()
            parts = form.get("parts")
            tools = form.get("tools")
            if any(value is not None and len(value.strip()) > 3000 for value in (parts, tools)):
                return error_page(request, "Детали и инструменты — не более 3000 символов каждый.", f"/requests/{request_id}")
            price = parse_client_price(form.get("price", ""))
            status = form.get("status", repair_request.status)
            if not 3 <= len(diagnosis) <= 3000 or price is None:
                return error_page(request, "Проверьте диагноз и сумму (до двух знаков после запятой).", "/panel")
            if status not in {"new", "in_progress", "completed", "cancelled"}:
                return error_page(request, "Недопустимый статус заявки.", "/panel")
            await add_request_staff_edit(db, request_id, user.id, diagnosis, price, parts=parts.strip() if parts is not None else None, tools=tools.strip() if tools is not None else None)
            await update_request_status(db, request_id, status)
        if status != previous_status:
            await notify_client_status(owner_telegram_id, request_id, status)
        return RedirectResponse("/panel", status_code=303)

    @app.post("/admin/users/{user_id}/role")
    async def admin_change_role(request: Request, user_id: int) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return error_page(request, "Сессия формы устарела. Обновите страницу.", "/panel")
        async with SessionLocal() as db:
            account, actor = await identity(request, db)
            if account is None or actor is None or actor.role != "admin":
                return forbidden_page(request)
            target = await db.get(User, user_id)
            new_role = form.get("role", "")
            if target is None or new_role not in ROLE_NAMES:
                return error_page(request, "Пользователь или роль не найдены.", "/panel")
            configured_role = configured_role_for_telegram_id(int(target.telegram_id))
            if configured_role and new_role != configured_role:
                return error_page(
                    request,
                    "Начальная роль из доверенного списка сервера закреплена за этим аккаунтом.",
                    "/admin/users",
                )
            if target.role == "admin" and new_role != "admin":
                admins = await users_by_role(db, "admin")
                if len(admins) <= 1:
                    return error_page(request, "Нельзя убрать последнюю роль администратора.", "/panel")
            target.role = new_role
            await db.commit()
        return RedirectResponse("/admin/users", status_code=303)

    @app.get("/admin/users", response_class=HTMLResponse)
    async def admin_users(request: Request) -> Response:
        async with SessionLocal() as db:
            account, actor = await identity(request, db)
            if account is None or actor is None or actor.role != "admin":
                return forbidden_page(request)
            users = await get_all_users(db)
            selected_role = request.query_params.get("role", "")
            if selected_role in ROLE_NAMES:
                users = [item for item in users if item.role == selected_role]
        csrf = csrf_token(request)
        rows = []
        for item in users:
            role_options = "".join(
                f"<option value='{role}' {'selected' if item.role == role else ''}>{name}</option>"
                for role, name in ROLE_NAMES.items()
            )
            configured = configured_role_for_telegram_id(int(item.telegram_id))
            role_control = f"<span class='protected-role'>🔒 {ROLE_NAMES[configured]} — роль закреплена на сервере</span>" if configured else (
                f"<form method='post' action='/admin/users/{item.id}/role'><input type='hidden' name='csrf' value='{csrf}'><select name='role'>{role_options}</select><button>Сохранить роль</button></form>"
            )
            rows.append(f"<tr><td>{html.escape(item.full_name)}</td><td>{html.escape(item.phone)}</td><td>{html.escape(item.telegram_id)}</td><td>{role_control}</td></tr>")
        content = (
            "<section class='card'><h2>Пользователи и роли</h2><p>Роль хранится на сервере и проверяется при каждом запросе.</p>"
            "<nav class='user-role-tabs' aria-label='Категории пользователей'><a href='/admin/users'>Все</a><a href='/admin/users?role=admin'>Администраторы</a><a href='/admin/users?role=master'>Мастера</a><a href='/admin/users?role=client'>Клиенты</a></nav>"
            "<div class='table-wrap'><table class='users-table'><thead><tr><th>Имя</th><th>Телефон</th><th>Telegram ID</th><th>Роль</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div></section>"
        )
        return render_page(request, "Управление ролями", content)

    @app.get("/admin/requests", response_class=HTMLResponse)
    async def admin_requests(request: Request) -> Response:
        async with SessionLocal() as db:
            account, actor = await identity(request, db)
            if account is None or actor is None or actor.role != "admin":
                return forbidden_page(request)
            requests = await get_all_requests(db, trash=request.query_params.get("trash") == "1")
        params = dict(request.query_params)
        return render_page(request, "Все заявки", "<h1>Заявки</h1>" + request_tabs(params) + transfer_controls(params) + requests_table(requests, staff=True, params=params, path="/admin/requests"))

    @app.get("/admin/certificates")
    async def admin_certificates(request: Request) -> Response:
        async with SessionLocal() as db:
            _account, actor = await identity(request, db)
            if actor is None or actor.role != "admin":
                return forbidden_page(request)
            gifts = list((await db.scalars(select(GiftCertificate).order_by(GiftCertificate.created_at.desc()))).all())
            owners = {user.id: user.full_name for user in await get_all_users(db)}
        return render_page(request, "Подарочные сертификаты", templates.get_template("admin_certificates.html").render(gifts=gifts, owners=owners))

    @app.post("/admin/tariffs/{code}")
    async def admin_update_tariff(request: Request, code: str) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return error_page(request, "Сессия формы устарела. Обновите страницу.", "/panel")
        async with SessionLocal() as db:
            account, actor = await identity(request, db)
            if account is None or actor is None or actor.role != "admin":
                return forbidden_page(request)
            amount = parse_client_price(form.get("surcharge", ""))
            label = form.get("label", "").strip()
            if amount is None or amount < 0 or not 2 <= len(label) <= 80:
                return error_page(request, "Проверьте название тарифа и доплату.", "/panel")
            if await update_urgency_tariff(db, code, label=label, surcharge_kopecks=amount) is None:
                return error_page(request, "Тариф не найден.", "/panel")
        return RedirectResponse("/admin/tariffs", status_code=303)

    @app.get("/admin/tariffs", response_class=HTMLResponse)
    async def admin_tariffs(request: Request) -> Response:
        async with SessionLocal() as db:
            account, actor = await identity(request, db)
            if account is None or actor is None or actor.role != "admin":
                return forbidden_page(request)
            tariffs = await get_urgency_tariffs(db)
        csrf = csrf_token(request)
        forms = []
        for tariff in tariffs:
            forms.append(
                f"<form class='tariff' method='post' action='/admin/tariffs/{tariff.code}'>"
                f"<input type='hidden' name='csrf' value='{csrf}'>"
                f"<label>Название<input name='label' value='{html.escape(tariff.label, quote=True)}' required></label>"
                f"<label>Доплата, ₽<input name='surcharge' type='number' min='0' step='0.01' value='{tariff.surcharge_kopecks / 100:.2f}' required></label>"
                "<button>Сохранить</button></form>"
            )
        return render_page(
            request,
            "Тарифы срочности",
            "<section class='card'><h2>Доплаты за срочность</h2>"
            + "".join(forms)
            + "<p>Уже созданные заявки сохраняют снимок цены на момент оформления.</p></section>",
        )

    @app.get("/bot")
    async def open_bot() -> Response:
        if BOT_USERNAME:
            return RedirectResponse(f"https://t.me/{BOT_USERNAME}", status_code=303)
        token = os.getenv("BOT_TOKEN")
        if not token:
            return Response("Бот пока не настроен.", status_code=503)
        try:
            async with Bot(token) as bot:
                info = await bot.get_me()
                return RedirectResponse(f"https://t.me/{info.username}", status_code=303)
        except TelegramAPIError:
            return Response("Telegram временно недоступен. Попробуйте позже.", status_code=503)

    @app.post("/certificates/sync")
    async def sync_own_certificates(request: Request) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return forbidden_page(request)
        async with SessionLocal() as db:
            _account, user = await identity(request, db)
            if user is None:
                return forbidden_page(request)
            await sync_certificates(db, user.id)
        return RedirectResponse("/panel?tab=certificates", status_code=303)

    @app.post("/certificates/{certificate_id}/apply")
    async def apply_certificate(request: Request, certificate_id: int) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return forbidden_page(request)
        try:
            target_id = int(form.get("request_id", ""))
        except ValueError:
            return error_page(request, "Выберите заявку для применения сертификата.", "/panel?tab=certificates")
        async with SessionLocal() as db:
            _account, user = await identity(request, db)
            if user is None or not await reserve_certificate(db, user.id, certificate_id, target_id):
                return error_page(request, "Сертификат недоступен или заявка не подходит. Нужен следующий открытый ремонт с проверенной мастером ценой.", "/panel?tab=certificates")
        return RedirectResponse(f"/requests/{target_id}", status_code=303)

    @app.get("/bot-qr.png")
    async def bot_qr() -> Response:
        output = BytesIO()
        qrcode.make("https://pc-doctor.duckdns.org/bot").save(output, format="PNG")
        return Response(output.getvalue(), media_type="image/png")

    @app.post("/feedback")
    async def submit_feedback(request: Request) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return error_page(request, "Сессия формы устарела. Обновите страницу.", "/panel?tab=feedback")
        async with SessionLocal() as db:
            account, user = await identity(request, db)
            if account is None:
                return forbidden_page(request)
            message = form.get("message", "").strip()
            if not 10 <= len(message) <= 3000:
                return error_page(request, "Сообщение должно содержать от 10 до 3000 символов.", "/panel?tab=feedback")
            now = time.time()
            if now - request.session.get("feedback_sent_at", 0) < 60:
                return error_page(request, "Подождите минуту перед следующим сообщением.", "/panel?tab=feedback")
            recipients = [item.telegram_id for item in await users_by_role(db, "admin")]
            sender = f"{user.full_name if user else 'Пользователь сайта'}\nEmail: {account.email}"
            if user:
                sender += f"\nTelegram ID: {user.telegram_id}"
        if not await deliver_feedback(recipients, f"Обратная связь с сайта\n{sender}\n\n{message}"):
            return error_page(request, "Сообщение не доставлено. Попробуйте позже.", "/panel?tab=feedback")
        request.session["feedback_sent_at"] = now
        return render_page(request, "Сообщение отправлено", "<section class='card'><h1>Сообщение отправлено</h1><p>Администраторы получили ваше обращение. Для ответа указаны данные вашего профиля.</p><a class='button secondary' href='/panel?tab=feedback'>Вернуться</a></section>")

    @app.get("/requests/{request_id}")
    async def request_detail(request: Request, request_id: int) -> Response:
        async with SessionLocal() as db:
            _account, user = await identity(request, db)
            item = await get_repair_request(db, request_id, include_deleted=bool(user and user.role == "admin" and effective_view(request, user) == "admin"))
            if not can_view_request(request, user, item):
                return forbidden_page(request)
            view = effective_view(request, user)
            address = await db.scalar(select(RequestAddress).where(RequestAddress.request_id == request_id))
            text = admin_request_text(item) if view in {"admin", "master"} else client_request_text(item)
            photo = (
                f"<img class='request-photo' src='/requests/{item.id}/photo?view={view}' alt='Фотография к заявке №{item.id}'>"
                if request_photo_id(item) else "<p>Фотография к заявке не приложена.</p>"
            )
            content = f"<a class='button secondary' href='/panel?tab=requests&view={view}'>← К списку</a><article class='request-card'><h1>Заявка №{item.id}</h1>"
            if request.query_params.get("created") == "1":
                content += "<p class='dashboard-notice'>Заявка создана и сохранена. Мастер увидит ваше обращение.</p>"
            content += text.replace(chr(10), "<br>") + photo
            if address:
                content += f"<p>Адрес выезда: {html.escape(address.address)}</p>"
            deleted = bool(item.lifecycle and item.lifecycle.deleted_at)
            if user.role == "admin" and view == "admin":
                content += trash_control(item, csrf_token(request))
            if item.lifecycle and item.lifecycle.imported:
                content += "<p class='muted'>Импортированная заявка: исходные данные сохранены, автоматическая выдача сертификатов отключена.</p>"
            if not deleted and item.status in {"new", "in_progress"} and not item.ai_assessment and not (item.lifecycle and item.lifecycle.imported):
                content += f"<form method='post' action='/requests/{item.id}/ai' data-request-form><input type='hidden' name='csrf' value='{csrf_token(request)}'><p>Первая AI-попытка бесплатно, последующие — 30 ₽ после подтверждения оплаты администратором. При технической ошибке обратитесь за компенсацией.</p><button>Выполнить доступную AI-диагностику</button></form>"
            if not deleted and view in {"master", "admin"}:
                content += staff_request_form(item, csrf_token(request))
            if not deleted and item.status in {"new", "in_progress"} and not request_photo_id(item):
                content += f"<form method='post' enctype='multipart/form-data' action='/requests/{item.id}/photo' data-request-form><input type='hidden' name='csrf' value='{csrf_token(request)}'><h2>Добавить фотографию</h2><label>Фото неисправности<input name='photo' type='file' accept='image/jpeg,image/png,image/webp' required></label><p>JPG, PNG или WebP до 5 МБ. Фото получит мастер; готовая AI-оценка не перезаписывается.</p><button>Сохранить фото</button></form>"
            return render_page(request, f"Заявка №{item.id}", content + "</article>")

    @app.post("/requests/{request_id}/photo")
    async def attach_request_photo(request: Request, request_id: int) -> Response:
        async with SessionLocal() as db:
            _, user = await identity(request, db)
            item = await get_repair_request(db, request_id)
            if not can_view_request(request, user, item):
                return forbidden_page(request)
            if item.status not in {"new", "in_progress"} or request_photo_id(item):
                return error_page(request, "Фото уже приложено или заявка закрыта.", f"/requests/{request_id}")
            try:
                form, photo = await read_photo_form(request)
                if not verify_csrf(request, form):
                    return forbidden_page(request)
                if photo is None:
                    raise ValueError("Выберите фотографию.")
                photo = await asyncio.to_thread(normalize_photo, photo)
            except ValueError as exc:
                return error_page(request, str(exc), f"/requests/{request_id}")
            reference = await save_web_photo(photo)
            try:
                await set_request_photo(db, request_id, reference)
            except Exception:
                await discard_web_photo(reference)
                raise
        return RedirectResponse(f"/requests/{request_id}", status_code=303)

    @app.get("/requests/{request_id}/photo")
    async def request_photo(request: Request, request_id: int) -> Response:
        async with SessionLocal() as db:
            _account, user = await identity(request, db)
            item = await get_repair_request(db, request_id, include_deleted=bool(user and user.role == "admin" and effective_view(request, user) == "admin"))
            if not can_view_request(request, user, item):
                return Response(status_code=403)
            file_id = request_photo_id(item)
        if not file_id:
            return Response(status_code=404)
        try:
            data = await download_request_photo(file_id)
        except (TelegramAPIError, ValueError, OSError):
            return Response("Фото временно недоступно.", status_code=502)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    register_admin_tools(app, sys.modules[__name__])
    return app


app = create_app()


def hash_password(password: str) -> str:
    """Хеширует пароль стойкой встроенной функцией scrypt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Сравнивает пароль с хешем за постоянное время."""
    try:
        algorithm, salt_hex, digest_hex = stored_hash.split("$", maxsplit=2)
        if algorithm != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


async def read_form(request: Request) -> dict[str, str]:
    """Читает небольшую HTML-форму без привязки к python-multipart."""
    raw_body = (await request.body()).decode("utf-8", errors="replace")
    return {key: values[-1] for key, values in parse_qs(raw_body, keep_blank_values=True).items()}


def csrf_token(request: Request) -> str:
    """Возвращает привязанный к подписанной сессии токен формы."""
    token = request.session.get("csrf")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def verify_csrf(request: Request, form: dict[str, str]) -> bool:
    """Проверяет CSRF-маркер для всех изменяющих запросов сайта."""
    expected = request.session.get("csrf")
    supplied = form.get("csrf")
    return isinstance(expected, str) and isinstance(supplied, str) and hmac.compare_digest(expected, supplied)


def effective_view(request: Request, user: User) -> str:
    """Разрешает предпросмотр только настоящему администратору."""
    view = request.query_params.get("view")
    return view if user.role == "admin" and view in {"client", "master"} else user.role


def can_view_request(request: Request, user: User | None, item: RepairRequest | None) -> bool:
    """Проверяет владельца или сотрудника до выдачи заявки и фотографии."""
    return bool(user and item and (not item.lifecycle or item.lifecycle.deleted_at is None or (user.role == "admin" and effective_view(request, user) == "admin")) and (effective_view(request, user) in {"admin", "master"} or item.user_id == user.id))


async def download_request_photo(file_id: str) -> bytes:
    """Получает фото через сервер, не раскрывая Telegram-токен браузеру."""
    if file_id.startswith("web:"):
        return await read_web_photo(file_id)
    async with Bot(os.getenv("BOT_TOKEN", "")) as bot:
        output = BytesIO()
        await bot.download(file_id, destination=output)
        return output.getvalue()


async def deliver_feedback(recipients: list[str], text: str) -> bool:
    """Доставляет явно отправленное пользователем сообщение администраторам."""
    if not recipients or not os.getenv("BOT_TOKEN"):
        return False
    delivered = False
    async with Bot(os.environ["BOT_TOKEN"]) as bot:
        for recipient in set(recipients):
            try:
                await bot.send_message(recipient, text)
                delivered = True
            except TelegramAPIError:
                logger.info("Не удалось доставить обратную связь одному из администраторов.")
    return delivered




async def identity(
    request: Request, db: AsyncSession
) -> tuple[WebsiteAccount | None, User | None]:
    """Возвращает веб-аккаунт и связанного Telegram-пользователя."""
    account_id = request.session.get("account_id")
    if not isinstance(account_id, int):
        return None, None
    account = await db.get(WebsiteAccount, account_id)
    if account is None:
        request.session.clear()
        return None, None
    user = await db.get(User, account.user_id) if account.user_id is not None else None
    request.state.current_role = user.role if user is not None else "client"
    request.state.current_name = user.full_name if user is not None else account.email
    return account, user


async def account_by_email(db: AsyncSession, email: str) -> WebsiteAccount | None:
    result = await db.execute(
        select(WebsiteAccount).where(WebsiteAccount.email == email)
    )
    return result.scalar_one_or_none()


async def users_by_role(db: AsyncSession, role: str) -> list[User]:
    result = await db.execute(select(User).where(User.role == role))
    return list(result.scalars().all())


async def get_site_texts(db: AsyncSession) -> dict[str, str]:
    result = await db.execute(select(SiteText))
    saved = {item.key: item.value for item in result.scalars()}
    return {key: saved.get(key, value) for key, value in SITE_TEXT_DEFAULTS.items()}


async def unlinked_panel(
    request: Request, account: WebsiteAccount, db: AsyncSession
) -> str:
    """Показывает шаг привязки Telegram для клиентского аккаунта."""
    return templates.get_template("onboarding.html").render(account=account, csrf=csrf_token(request))



async def client_panel(
    request: Request, account: WebsiteAccount, user: User, db: AsyncSession
) -> str:
    """Рендерит клиентские заявки и форму создания новой."""
    csrf = csrf_token(request)
    requests = await get_user_requests(db, user.id)
    tariffs = await get_urgency_tariffs(db)
    texts = await get_site_texts(db)
    tariff_options = "".join(
        f"<option value='{item.code}'>{html.escape(item.label)} — "
        f"{format_rubles(item.surcharge_kopecks)}</option>"
        for item in tariffs
    )
    address_value = html.escape(account.address or "", quote=True)
    create_form = (
        "<section class='card'><h2>Создать заявку</h2><p>Первая AI-диагностика бесплатная, последующие — 30 ₽. Без AI-доступа заявка всё равно передаётся мастеру. Оплату проверяет администратор.</p><form method='post' action='/requests/create' enctype='multipart/form-data' data-request-form>"
        f"<input type='hidden' name='csrf' value='{csrf}'>"
        "<label>Описание проблемы<textarea name='description' minlength='10' maxlength='4000' required></textarea></label>"
        "<label>Фотография (необязательно)<input type='file' name='photo' accept='image/jpeg,image/png,image/webp'></label><p class='muted'>JPG, PNG или WebP до 5 МБ. Мастер увидит фото даже без AI-оценки.</p>"
        f"<label>Срочность<select name='urgency'>{tariff_options}</select></label>"
        f"<label>Адрес выезда (необязательно)<input name='address' maxlength='500' value='{address_value}'></label>"
        f"<p class='muted'>{html.escape(texts['service_note'])}</p><button>Отправить заявку</button></form></section>"
    )
    preview_note = "<p class='dashboard-notice'>Вы создаёте настоящую заявку от своего аккаунта. Роль администратора сохраняется.</p>" if user.role == "admin" else ""
    wallet = await get_wallet(db, user)
    await db.commit()
    ai_counter = "Безлимит по роли администратора" if user.role == "admin" else str(available_analyses(wallet))
    form_html = preview_note + ("<details class='create-request-panel' open>" if request.query_params.get("create") == "1" else "<details class='create-request-panel'>") + "<summary>＋ Создать заявку</summary>" + create_form + "</details>"
    return (
        "<section class='list-heading'><div><h1>Мои заявки</h1><p>Список обращений, статусы и предварительная стоимость ремонта.</p></div></section>"
        + f"<p class='dashboard-notice'>AI-диагностик осталось: {ai_counter}</p>"
        + form_html
        + requests_table(requests, view="client" if user.role == "admin" else "", params=dict(request.query_params))
    )



async def master_panel(request: Request, user: User, db: AsyncSession) -> str:
    """Рендерит рабочую очередь мастера с формами исправления."""
    requests = await get_all_requests(db)
    title = {"in_progress": "Заявки в работе", "closed": "Архив заявок"}.get(request.query_params.get("status"), "Очередь заявок")
    return (
        f"<section class='list-heading'><h1>{title}</h1><p>Диагностика, стоимость и статусы ремонта — в карточках обращений.</p></section>"
        + requests_table(requests, staff=True, view="master" if user.role == "admin" else "", params=dict(request.query_params))
    )



async def admin_panel(request: Request, user: User, db: AsyncSession) -> str:
    """Рендерит сводку и ссылки на инструменты администратора."""
    users = await get_all_users(db)
    items = await get_all_requests(db)
    return dashboard_content(request, user, "admin", items, users)



def dashboard_content(request: Request, user: User, role: str, items: list[RepairRequest], users: list[User]) -> str:
    """Готовит ролевую сводку только из доступных пользователю заявок."""
    preview = role if user.role == "admin" and role != "admin" else ""
    suffix = f"&view={preview}" if preview else ""
    list_url = "/admin/requests?tab=requests" if role == "admin" else "/panel?tab=requests" + suffix
    rows = []
    for item in items:
        price = request_price(item)
        rows.append({"id": item.id, "description": item.description[:140], "status": item.status,
                     "status_label": STATUSES.get(item.status, "Неизвестен"), "priority": item.priority,
                     "priority_label": PRIORITIES.get(item.priority, "Средний"),
                     "date": item.created_at.strftime("%d.%m.%Y"),
                     "price": format_rubles(price) if price is not None else "Уточняется",
                     "owner": item.user.full_name if role != "client" else "",
                     "url": f"/requests/{item.id}" + (f"?view={preview}" if preview else "")})
    active = [row for row in rows if row["status"] in {"new", "in_progress"}]
    counts = {key: sum(row["status"] == key for row in rows) for key in STATUSES}
    return templates.get_template("dashboard.html").render(
        role=role, name=user.full_name, phone=user.phone, rows=rows[:5], focus=active[0] if active else (rows[0] if rows else None),
        counts=counts, total=len(rows), active=len(active), high=sum(row["priority"] == "high" for row in active),
        unchecked=sum(item.status in {"new", "in_progress"} and not item.ai_assessment and not item.staff_edits for item in items),
        role_counts={key: sum(person.role == key for person in users) for key in ROLE_NAMES},
        list_url=list_url, suffix=suffix, preview=preview,
    )


def staff_request_form(item: RepairRequest, csrf: str) -> str:
    """Строит форму статуса и подтверждённой правки диагноза/цены."""
    latest = item.staff_edits[-1] if item.staff_edits else None
    parts, tools = effective_staff_details(item)
    diagnosis = latest.diagnosis if latest else (
        item.ai_assessment.master_diagnosis if item.ai_assessment else ""
    )
    price = latest.client_price_kopecks if latest else (
        item.ai_assessment.client_price_kopecks if item.ai_assessment else None
    )
    price_value = f"{price / 100:.2f}" if price is not None else ""
    status_options = "".join(
        f"<option value='{status}' {'selected' if item.status == status else ''}>{label}</option>"
        for status, label in (
            ("new", "Новая"),
            ("in_progress", "В работе"),
            ("completed", "Завершена"),
            ("cancelled", "Отменена"),
        )
    )
    return (
        f"<form method='post' action='/staff/requests/{item.id}/update'>"
        f"<input type='hidden' name='csrf' value='{csrf}'>"
        f"<label>Подтверждённый диагноз<textarea name='diagnosis' minlength='3' maxlength='3000' required>{html.escape(diagnosis)}</textarea></label>"
        f"<label>Детали для проверки<textarea name='parts' maxlength='3000'>{html.escape(parts)}</textarea></label>"
        f"<label>Инструменты<textarea name='tools' maxlength='3000'>{html.escape(tools)}</textarea></label>"
        f"<label>Стоимость ремонта, ₽<input name='price' type='number' min='0' step='0.01' value='{price_value}' required></label>"
        f"<label>Статус<select name='status'>{status_options}</select></label>"
        "<button>Сохранить проверку и статус</button></form>"
    )


def auth_form(request: Request, mode: str) -> str:
    """Создает форму регистрации или входа."""
    csrf = csrf_token(request)
    is_register = mode == "register"
    introduction = "<p>Войдите или создайте аккаунт, чтобы отправить заявку на сайте. После входа вы продолжите оформление.</p>" if request.session.get("after_auth") == "request" else ""
    fields = (
        "<label>Повторите пароль<input type='password' name='password_repeat' autocomplete='new-password' required></label>"
        if is_register
        else ""
    )
    return (
        "<section class='auth-card'><h1>"
        + ("Создать аккаунт" if is_register else "Вход в кабинет")
        + f"</h1>{introduction}<form method='post' action='/{mode}'>"
        f"<input type='hidden' name='csrf' value='{csrf}'>"
        "<label>Email<input type='email' name='email' maxlength='254' autocomplete='email' required></label>"
        f"<label>Пароль<input type='password' name='password' minlength='8' "
        f"autocomplete='{'new-password' if is_register else 'current-password'}' required></label>"
        f"{fields}<button>{'Зарегистрироваться' if is_register else 'Войти'}</button></form>"
        f"<p>{'Уже есть аккаунт?' if is_register else 'Нет аккаунта?'} "
        f"<a href='/{'login' if is_register else 'register'}'>{'Войти' if is_register else 'Зарегистрироваться'}</a></p></section>"
    )


def render_page(request: Request, title: str, content: str) -> HTMLResponse:
    """Рендерит страницу через общий Jinja-шаблон кабинета."""
    logged_in = isinstance(request.session.get("account_id"), int)
    csrf = csrf_token(request)
    logout = (
        f"<form method='post' action='/logout'><input type='hidden' name='csrf' value='{csrf}'>"
        "<button class='logout'>Выйти</button></form>"
        if logged_in
        else ""
    )
    role = getattr(request.state, "current_role", None)
    actual_role = role
    if role == "admin" and request.query_params.get("view") in {"client", "master"}:
        role = request.query_params["view"]
    nav_items = navigation_items(role, request.url.path, request.query_params.get("tab", "overview"), request.query_params.get("view", "") if actual_role == "admin" else "", request.query_params.get("status", "")) if logged_in else []
    if logged_in and not nav_items:
        nav_items = [{"label": "Личный кабинет", "href": "/panel", "active": False}]
    return templates.TemplateResponse(
        request=request,
        name="base.html",
        context={
            "title": title,
            "content": content,
            "logged_in": logged_in,
            "logout": logout,
            "nav_items": nav_items,
            "is_admin": actual_role == "admin",
            "bot_available": bool(os.getenv("BOT_TOKEN")),
            "account_name": getattr(request.state, "current_name", ""),
        },
    )


def navigation_items(role: str | None, path: str, tab: str = "overview", preview: str = "", status: str = "") -> list[dict[str, str | bool]]:
    """Формирует рабочие вкладки с сохранением предпросмотра роли."""
    common = [("Главная", "/panel?tab=overview"), ("Профиль", "/panel?tab=profile"), ("Обратная связь", "/panel?tab=feedback")]
    options = {
        "client": [common[0], ("Мои заявки", "/panel?tab=requests"), ("Услуги и цены", "/panel?tab=tariffs"), ("Сертификаты", "/panel?tab=certificates"), *common[1:]],
        "master": [common[0], ("Очередь", "/panel?tab=requests"), ("В работе", "/panel?tab=requests&status=in_progress"), ("Архив", "/panel?tab=requests&status=closed"), *common[1:]],
        "admin": [common[0], ("Заявки", "/admin/requests"), ("Пользователи", "/admin/users"), ("Сертификаты", "/admin/certificates"), ("AI-доступ", "/admin/ai"), ("Тарифы", "/admin/tariffs"), common[1]],
    }
    result = []
    for label, href in options.get(role or "", []):
        if "?tab=" in href:
            active = path == "/panel" and f"tab={tab}" in href
            if tab == "requests":
                expected_status = "in_progress" if label == "В работе" else "closed" if label == "Архив" else ""
                active = active and status == expected_status
        else:
            active = path == href
        if preview in {"client", "master"}:
            href += ("&" if "?" in href else "?") + "view=" + preview
        result.append({"label":label, "href":href, "active":active})
    return result


def public_home(logged_in: bool = False) -> str:
    """Формирует главную страницу без вымышленных отзывов и цен."""
    bot_url = "/request/new"
    return templates.get_template("home.html").render(bot_url=bot_url)



def error_page(request: Request, message: str, back: str) -> HTMLResponse:
    """Показывает безопасное сообщение и ссылку возврата."""
    return render_page(
        request,
        "Проверьте данные",
        f"<section class='card'><h1>Не удалось выполнить действие</h1>"
        f"<p>{html.escape(message)}</p><a class='button' href='{html.escape(back)}'>Назад</a></section>",
    )


def forbidden_page(request: Request) -> HTMLResponse:
    """Возвращает отказ в доступе, не раскрывая чужие данные."""
    page = error_page(
        request, "У вашей учётной записи нет доступа к этому разделу.", "/panel"
    )
    page.status_code = 403
    return page


async def notify_client_status(
    telegram_id: str, request_id: int, status: str
) -> None:
    """Уведомляет клиента об изменении статуса, если настроен Telegram-токен."""
    token = os.getenv("BOT_TOKEN")
    status_messages = {
        "in_progress": "🔧 Ваша заявка принята в работу.",
        "completed": "✅ Ваша заявка завершена.",
        "cancelled": "❌ Ваша заявка отменена.",
        "new": "🆕 Статус вашей заявки изменён на «Новая».",
    }
    if not token or status not in status_messages:
        return
    bot = None
    try:
        bot = Bot(token=token)
        await bot.send_message(
            chat_id=telegram_id,
            text=f"{status_messages[status]} Номер заявки: {request_id}.",
        )
    except (TelegramAPIError, ValueError):
        logger.info("Не удалось отправить уведомление для заявки №%s.", request_id)
    finally:
        if bot is not None:
            await bot.session.close()
