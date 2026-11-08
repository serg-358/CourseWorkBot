"""Веб-кабинет с отдельным доступом для каждой роли."""

import hashlib
import html
import hmac
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
    save_ai_assessment,
    update_request_status,
)
from repair_bot.database.crud.users import get_all_users
from repair_bot.database.models import (
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
from repair_bot.services.pricing.catalog import calculate_cost

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


def create_app() -> FastAPI:
    """Создает отдельное ASGI-приложение сайта."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if os.getenv("ENVIRONMENT") == "production" and len(SESSION_SECRET) < 32:
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

    @app.get("/health", response_class=Response)
    async def health() -> Response:
        return Response("ok", media_type="text/plain")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        return RedirectResponse("/panel" if request.session.get("account_id") else "/login")

    @app.get("/register", response_class=HTMLResponse)
    async def register_page(request: Request) -> HTMLResponse:
        return render_page(request, "Регистрация", auth_form(request, "register"))

    @app.post("/register")
    async def register(request: Request) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return error_page(request, "Сессия формы устарела. Обновите страницу.", "/register")
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
            return error_page(request, "Введите корректный адрес электронной почты.", "/register")
        if len(password) < 12 or len(password) > 128:
            return error_page(request, "Пароль должен содержать от 12 до 128 символов.", "/register")
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
        request.session.clear()
        request.session["account_id"] = account.id
        return RedirectResponse("/panel", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
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
        request.session.clear()
        request.session["account_id"] = account.id
        return RedirectResponse("/panel", status_code=303)

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
            if user is None:
                content = await unlinked_panel(request, account, db)
            elif user.role == "client":
                content = await client_panel(request, account, user, db)
            elif user.role == "master":
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
        bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else None
        link_content = (
            "<section class='card'><h2>Привязка Telegram</h2>"
            "<p>Откройте бота, завершите регистрацию, затем отправьте команду:</p>"
            f"<pre>/link {html.escape(code)}</pre>"
            "<p>Код одноразовый и действует 10 минут.</p>"
            + (f"<a class='button' href='{html.escape(bot_link)}'>Открыть Telegram-бота</a>" if bot_link else "")
            + "</section>"
        )
        return render_page(request, "Привязка Telegram", link_content)

    @app.post("/requests/create")
    async def create_request_from_site(request: Request) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return error_page(request, "Сессия формы устарела. Обновите страницу.", "/panel")
        async with SessionLocal() as db:
            account, user = await identity(request, db)
            if account is None or user is None or user.role != "client":
                return forbidden_page(request)
            description = form.get("description", "").strip()
            address = form.get("address", "").strip()
            if not 10 <= len(description) <= 4000:
                return error_page(request, "Опишите проблему минимум 10 символами.", "/panel")
            if len(address) > 500:
                return error_page(request, "Адрес не должен превышать 500 символов.", "/panel")
            tariff = await get_urgency_tariff(db, form.get("urgency", "ordinary"))
            if tariff is None:
                return error_page(request, "Выбранный тариф недоступен.", "/panel")
            repair_request = await create_repair_request(
                db, user.id, description, tariff.code
            )
            if address:
                db.add(RequestAddress(request_id=repair_request.id, address=address))
                account.address = address
                await db.commit()
            try:
                ai_result = await analyze_repair_request(description, None)
                if ai_result is not None:
                    items, cost, price = calculate_cost(ai_result.catalog_item_codes)
                    codes = [item.code for item in items] if cost is not None else []
                    await save_ai_assessment(
                        db,
                        repair_request.id,
                        priority=ai_result.priority,
                        photo_file_id=None,
                        client_causes=ai_result.client_causes,
                        client_recommendations=ai_result.client_recommendations,
                        safety_note=ai_result.safety_note,
                        master_diagnosis=ai_result.master_diagnosis,
                        parts=ai_result.parts,
                        tools=ai_result.tools,
                        catalog_item_codes=",".join(codes),
                        estimated_cost_kopecks=cost,
                        client_price_kopecks=price,
                    )
            except Exception:
                logger.exception("AI-оценка веб-заявки №%s недоступна", repair_request.id)
        return RedirectResponse("/panel", status_code=303)

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
            price = parse_client_price(form.get("price", ""))
            status = form.get("status", repair_request.status)
            if not 3 <= len(diagnosis) <= 3000 or price is None:
                return error_page(request, "Проверьте диагноз и сумму (до двух знаков после запятой).", "/panel")
            if status not in {"new", "in_progress", "completed", "cancelled"}:
                return error_page(request, "Недопустимый статус заявки.", "/panel")
            await add_request_staff_edit(db, request_id, user.id, diagnosis, price)
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
        csrf = csrf_token(request)
        rows = []
        for item in users:
            role_options = "".join(
                f"<option value='{role}' {'selected' if item.role == role else ''}>{name}</option>"
                for role, name in ROLE_NAMES.items()
            )
            rows.append(
                "<tr>"
                f"<td>{html.escape(item.full_name)}</td><td>{html.escape(item.phone)}</td>"
                f"<td>{html.escape(item.telegram_id)}</td><td><form method='post' action='/admin/users/{item.id}/role'>"
                f"<input type='hidden' name='csrf' value='{csrf}'><select name='role'>{role_options}</select>"
                "<button>Сохранить</button></form></td></tr>"
            )
        content = (
            "<section class='card'><h2>Пользователи и роли</h2><p>Роль хранится на сервере и проверяется при каждом запросе.</p>"
            "<div class='table-wrap'><table><thead><tr><th>Имя</th><th>Телефон</th><th>Telegram ID</th><th>Роль</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div></section>"
        )
        return render_page(request, "Управление ролями", content)

    @app.get("/admin/requests", response_class=HTMLResponse)
    async def admin_requests(request: Request) -> Response:
        async with SessionLocal() as db:
            account, actor = await identity(request, db)
            if account is None or actor is None or actor.role != "admin":
                return forbidden_page(request)
            requests = await get_all_requests(db)
            address_result = await db.execute(
                select(RequestAddress).where(
                    RequestAddress.request_id.in_([item.id for item in requests])
                )
            )
            addresses = {
                item.request_id: item for item in address_result.scalars().all()
            }
        cards = []
        for item in requests:
            address = addresses.get(item.id)
            address_text = (
                f"<p><b>Адрес выезда:</b> {html.escape(address.address)}</p>"
                if address
                else ""
            )
            cards.append(
                f"<article class='request-card'><h2>Заявка №{item.id} · "
                f"{html.escape(item.user.full_name)} · {html.escape(item.status)}</h2>"
                f"{admin_request_text(item).replace(chr(10), '<br>')}{address_text}"
                f"{staff_request_form(item, csrf_token(request))}</article>"
            )
        return render_page(
            request,
            "Все заявки",
            "<section><h1>База заявок</h1>"
            + ("".join(cards) or "<article class='card muted'>Заявок пока нет.</article>")
            + "</section>",
        )

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

    @app.post("/admin/texts")
    async def admin_update_texts(request: Request) -> Response:
        form = await read_form(request)
        if not verify_csrf(request, form):
            return error_page(request, "Сессия формы устарела. Обновите страницу.", "/panel")
        async with SessionLocal() as db:
            account, actor = await identity(request, db)
            if account is None or actor is None or actor.role != "admin":
                return forbidden_page(request)
            for key in SITE_TEXT_DEFAULTS:
                value = form.get(key, "").strip()
                if not 1 <= len(value) <= 1000:
                    return error_page(request, "Текст должен содержать до 1000 символов.", "/panel")
                text_item = await db.get(SiteText, key)
                if text_item is None:
                    db.add(SiteText(key=key, value=value))
                else:
                    text_item.value = value
            await db.commit()
        return RedirectResponse("/admin/texts", status_code=303)

    @app.get("/admin/texts", response_class=HTMLResponse)
    async def admin_texts(request: Request) -> Response:
        async with SessionLocal() as db:
            account, actor = await identity(request, db)
            if account is None or actor is None or actor.role != "admin":
                return forbidden_page(request)
            values = await get_site_texts(db)
        csrf = csrf_token(request)
        fields = "".join(
            f"<label>{html.escape(key)}<textarea name='{key}' required>{html.escape(value)}</textarea></label>"
            for key, value in values.items()
        )
        return render_page(
            request,
            "Тексты сайта",
            "<section class='card'><h2>Управляемые тексты</h2><form method='post' action='/admin/texts'>"
            f"<input type='hidden' name='csrf' value='{csrf}'>{fields}<button>Сохранить</button></form></section>",
        )

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
    csrf = csrf_token(request)
    message = (
        "<p>Чтобы видеть свои заявки, зарегистрируйтесь в Telegram-боте, "
        "затем создайте одноразовый код привязки.</p>"
    )
    bot_link = f"<a href='https://t.me/{html.escape(BOT_USERNAME)}'>Открыть бота</a>" if BOT_USERNAME else ""
    return (
        f"<section class='hero'><h1>Здравствуйте!</h1><p>{html.escape(account.email)}</p></section>"
        f"<section class='card'><h2>Свяжите аккаунт с Telegram</h2>{message}{bot_link}"
        "<form method='post' action='/account/link-code'>"
        f"<input type='hidden' name='csrf' value='{csrf}'><button>Получить одноразовый код</button></form></section>"
    )


async def client_panel(
    request: Request, account: WebsiteAccount, user: User, db: AsyncSession
) -> str:
    """Рендерит клиентские заявки и форму создания новой."""
    csrf = csrf_token(request)
    requests = await get_user_requests(db, user.id)
    tariffs = await get_urgency_tariffs(db)
    texts = await get_site_texts(db)
    cards = []
    for item in requests:
        card = client_request_text(item)
        address_row = await db.execute(
            select(RequestAddress).where(RequestAddress.request_id == item.id)
        )
        address = address_row.scalar_one_or_none()
        extra = (
            f"<p><b>Адрес выезда:</b> {html.escape(address.address)}</p>" if address else ""
        )
        cards.append(
            f"<article class='request-card'>{card.replace(chr(10), '<br>')}{extra}</article>"
        )
    tariff_options = "".join(
        f"<option value='{item.code}'>{html.escape(item.label)} — "
        f"{format_rubles(item.surcharge_kopecks)}</option>"
        for item in tariffs
    )
    address_value = html.escape(account.address or "", quote=True)
    create_form = (
        "<section class='card'><h2>Создать заявку</h2><form method='post' action='/requests/create'>"
        f"<input type='hidden' name='csrf' value='{csrf}'>"
        "<label>Описание проблемы<textarea name='description' minlength='10' maxlength='4000' required></textarea></label>"
        f"<label>Срочность<select name='urgency'>{tariff_options}</select></label>"
        f"<label>Адрес выезда (необязательно)<input name='address' maxlength='500' value='{address_value}'></label>"
        f"<p class='muted'>{html.escape(texts['service_note'])}</p><button>Отправить заявку</button></form></section>"
    )
    bot_link = f"<a href='https://t.me/{html.escape(BOT_USERNAME)}'>Открыть Telegram-бота</a>" if BOT_USERNAME else ""
    return (
        f"<section class='hero'><h1>Кабинет клиента</h1><p>Здравствуйте, {html.escape(user.full_name)}.</p>"
        f"<p>Телефон: {html.escape(user.phone)}</p>"
        f"<p>{html.escape(texts['client_welcome'])}</p>{bot_link}</section>"
        + create_form
        + "<section><h2>Ваши заявки</h2>"
        + ("".join(cards) or "<article class='card muted'>Заявок пока нет.</article>")
        + "</section>"
    )


async def master_panel(request: Request, user: User, db: AsyncSession) -> str:
    """Рендерит рабочую очередь мастера с формами исправления."""
    requests = await get_all_requests(db)
    cards = []
    for item in requests:
        address_row = await db.execute(
            select(RequestAddress).where(RequestAddress.request_id == item.id)
        )
        address = address_row.scalar_one_or_none()
        address_text = (
            f"<p><b>Адрес выезда:</b> {html.escape(address.address)}</p>" if address else ""
        )
        cards.append(
            f"<article class='request-card'><h2>Заявка №{item.id} · {html.escape(item.user.full_name)}</h2>"
            f"{admin_request_text(item).replace(chr(10), '<br>')}{address_text}"
            f"{staff_request_form(item, csrf_token(request))}</article>"
        )
    return (
        f"<section class='hero'><h1>Панель мастера</h1><p>Здравствуйте, {html.escape(user.full_name)}. "
        f"Заявок в очереди: {len(requests)}.</p></section>"
        + ("".join(cards) or "<article class='card muted'>Новых заявок пока нет.</article>")
    )


async def admin_panel(request: Request, user: User, db: AsyncSession) -> str:
    """Рендерит сводку и ссылки на инструменты администратора."""
    users = await get_all_users(db)
    requests = await get_all_requests(db)
    return (
        f"<section class='hero'><h1>Панель администратора</h1>"
        f"<p>Здравствуйте, {html.escape(user.full_name)}.</p>"
        f"<div class='stats'><div><b>{len(users)}</b><span>Пользователей</span></div>"
        f"<div><b>{len(requests)}</b><span>Заявок</span></div></div></section>"
        "<nav class='grid-nav'><a href='/admin/users'>Пользователи и роли</a>"
        "<a href='/admin/requests'>Все заявки и цены</a>"
        "<a href='/admin/tariffs'>Тарифы срочности</a><a href='/admin/texts'>Тексты сайта</a></nav>"
    )


def staff_request_form(item: RepairRequest, csrf: str) -> str:
    """Строит форму статуса и подтверждённой правки диагноза/цены."""
    latest = item.staff_edits[-1] if item.staff_edits else None
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
        f"<label>Стоимость ремонта, ₽<input name='price' type='number' min='0' step='0.01' value='{price_value}' required></label>"
        f"<label>Статус<select name='status'>{status_options}</select></label>"
        "<button>Сохранить проверку и статус</button></form>"
    )


def auth_form(request: Request, mode: str) -> str:
    """Создает форму регистрации или входа."""
    csrf = csrf_token(request)
    is_register = mode == "register"
    fields = (
        "<label>Повторите пароль<input type='password' name='password_repeat' autocomplete='new-password' required></label>"
        if is_register
        else ""
    )
    return (
        "<section class='auth-card'><h1>"
        + ("Создать аккаунт" if is_register else "Вход в кабинет")
        + f"</h1><form method='post' action='/{mode}'>"
        f"<input type='hidden' name='csrf' value='{csrf}'>"
        "<label>Email<input type='email' name='email' maxlength='254' autocomplete='email' required></label>"
        f"<label>Пароль<input type='password' name='password' minlength='12' maxlength='128' "
        f"autocomplete='{'new-password' if is_register else 'current-password'}' required></label>"
        f"{fields}<button>{'Зарегистрироваться' if is_register else 'Войти'}</button></form>"
        f"<p>{'Уже есть аккаунт?' if is_register else 'Нет аккаунта?'} "
        f"<a href='/{'login' if is_register else 'register'}'>{'Войти' if is_register else 'Зарегистрироваться'}</a></p></section>"
    )


def render_page(request: Request, title: str, content: str) -> HTMLResponse:
    """Оборачивает экран в адаптивный HTML-шаблон."""
    logged_in = isinstance(request.session.get("account_id"), int)
    csrf = csrf_token(request)
    logout = (
        f"<form method='post' action='/logout'><input type='hidden' name='csrf' value='{csrf}'>"
        "<button class='logout'>Выйти</button></form>"
        if logged_in
        else ""
    )
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} · ПК Доктор</title>
<style>
:root{{color-scheme:dark;--bg:#101411;--panel:#1a211c;--line:#303c33;--green:#86cf91;--text:#f2f5f1;--muted:#a7b2a8}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(ellipse at top,#24392a,var(--bg) 55%);color:var(--text);font:16px/1.5 system-ui,Segoe UI,sans-serif}}
a{{color:var(--green)}}header{{height:68px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 max(20px,calc((100vw - 1120px)/2))}}
.brand{{font-weight:800;color:var(--text);text-decoration:none;font-size:20px}}main{{max-width:1120px;margin:34px auto;padding:0 20px 60px}}h1{{font-size:clamp(28px,4vw,42px);margin:0 0 12px}}h2{{margin:0 0 14px}}.hero,.card,.request-card,.auth-card{{background:linear-gradient(140deg,#1b251e,#151a16);border:1px solid var(--line);border-radius:18px;padding:24px;margin:0 0 18px;box-shadow:0 12px 35px #0003}}
.hero{{border-color:#405c45}}.muted{{color:var(--muted)}}label{{display:block;margin:14px 0;color:var(--muted)}}input,textarea,select{{display:block;width:100%;margin-top:6px;background:#101511;border:1px solid #3a493d;border-radius:10px;padding:12px;color:var(--text);font:inherit}}textarea{{min-height:100px;resize:vertical}}button,.button{{display:inline-block;border:0;border-radius:10px;padding:11px 17px;background:var(--green);color:#102013;font:700 15px system-ui;text-decoration:none;cursor:pointer}}button:hover,.button:hover{{filter:brightness(1.08)}}.logout{{background:#303b32;color:var(--text);padding:8px 12px}}.auth-card{{max-width:460px;margin:8vh auto}}.stats{{display:flex;gap:16px;margin-top:20px}}.stats div{{background:#111813;padding:15px 20px;border-radius:12px;min-width:140px}}.stats b,.stats span{{display:block}}.stats b{{font-size:28px;color:var(--green)}}.grid-nav{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}}.grid-nav a{{padding:20px;border:1px solid var(--line);background:var(--panel);border-radius:14px;text-decoration:none;font-weight:700}}.request-card form,.tariff{{margin-top:18px;padding-top:15px;border-top:1px solid var(--line)}}.tariff{{display:grid;grid-template-columns:1fr 1fr auto;gap:12px;align-items:end}}.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse}}td,th{{padding:12px;text-align:left;border-bottom:1px solid var(--line)}}td form{{display:flex;gap:8px;min-width:280px}}pre{{padding:14px;background:#0c100d;border-radius:8px;overflow:auto}}@media(max-width:640px){{main{{margin-top:18px}}.hero,.card,.request-card{{padding:18px}}.tariff{{grid-template-columns:1fr}}}}
</style></head><body><header><a class="brand" href="/panel">🖥 ПК Доктор</a>{logout}</header><main>{content}</main></body></html>"""
    return HTMLResponse(page)


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
