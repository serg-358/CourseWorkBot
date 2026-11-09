"""Защищённые инструменты переноса, корзины и оплаченных AI-попыток."""

import html
import secrets
import re
from urllib.parse import urlencode
from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from repair_bot.database.models import AiCreditGrant, RequestAddress, User
from repair_bot.database.crud.ai_access import get_wallet, grant_analysis, available_analyses, set_analysis_balance
from repair_bot.database.crud.request_lifecycle import move_to_trash, request_photo_id
from repair_bot.database.crud.request_transfer import preview_import, confirm_import
from repair_bot.database.crud.requests import get_all_requests, get_repair_request
from repair_bot.services.ai.budget import analyze_with_budget
from repair_bot.services.request_transfer import export_row, serialize_rows
from repair_bot.web.workspace import sorted_requests, STATUSES, PRIORITIES


def transfer_controls(params: dict[str, str]) -> str:
    """Экспорт использует те же фильтры, что видимая таблица."""
    links = " ".join(f"<a class='button secondary' href='/admin/requests/export?{html.escape(urlencode({**params, 'format': kind}), quote=True)}'>Экспорт {kind.upper()}</a>" for kind in ("json", "csv"))
    return f"<div class='request-filters'>{links}<a class='button secondary' href='/admin/requests/import'>Импорт с предпросмотром</a><a class='button secondary' href='/admin/ai'>AI-доступ</a></div>"


def request_tabs(params: dict[str, str]) -> str:
    choices = (("", "Все"), ("new", "Новые"), ("in_progress", "В работе"), ("completed", "Завершённые"), ("cancelled", "Отменённые"), ("trash", "Корзина"))
    links = []
    for status, label in choices:
        query = {**params, "status": "" if status == "trash" else status, "trash": "1" if status == "trash" else "0"}
        active = params.get("trash") == "1" if status == "trash" else params.get("trash") != "1" and params.get("status", "") == status
        links.append(f"<a {'aria-current=page' if active else ''} href='/admin/requests?{html.escape(urlencode(query), quote=True)}'>{label}</a>")
    return "<nav class='user-role-tabs' aria-label='Статусы заявок'>" + "".join(links) + "</nav>"


def trash_control(item, csrf: str) -> str:
    deleted = item.lifecycle and item.lifecycle.deleted_at
    action, label = ("restore", "Восстановить заявку") if deleted else ("trash", "Переместить в корзину")
    notice = f"<p class='dashboard-notice'>В корзине с {item.lifecycle.deleted_at:%d.%m.%Y %H:%M}. Изменения отключены. История сохранена.</p>" if deleted else ""
    return notice + f"<form method='post' action='/admin/requests/{item.id}/{action}'><input type='hidden' name='csrf' value='{csrf}'><button class='secondary'>{label}</button></form>"


def register_admin_tools(app, web) -> None:
    """Зависимости берутся из веб-модуля при запросе, включая тестовую фабрику БД."""
    @app.post("/admin/requests/{request_id}/{operation}")
    async def trash_request(request: Request, request_id: int, operation: str):
        form = await web.read_form(request)
        if operation not in {"trash", "restore"} or not web.verify_csrf(request, form):
            return web.forbidden_page(request)
        async with web.SessionLocal() as db:
            _, actor = await web.identity(request, db)
            if not actor or actor.role != "admin" or not await move_to_trash(db, request_id, actor, restore=operation == "restore"):
                return web.forbidden_page(request)
        return RedirectResponse("/admin/requests?trash=" + ("0" if operation == "restore" else "1"), status_code=303)

    @app.get("/admin/requests/export")
    async def export_requests(request: Request):
        async with web.SessionLocal() as db:
            _, actor = await web.identity(request, db)
            if not actor or actor.role != "admin":
                return web.forbidden_page(request)
            kind = request.query_params.get("format", "json")
            items = sorted_requests(await get_all_requests(db, trash=request.query_params.get("trash") == "1"), dict(request.query_params))
            addresses = dict((await db.execute(select(RequestAddress.request_id, RequestAddress.address))).all())
            try:
                payload = serialize_rows([export_row(i, addresses.get(i.id, "")) for i in items], kind)
            except ValueError as exc:
                return web.error_page(request, str(exc), "/admin/requests")
        return Response(payload, media_type="application/json" if kind == "json" else "text/csv", headers={"Content-Disposition": f'attachment; filename="repair-requests.{kind}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/admin/requests/import")
    async def import_form(request: Request):
        async with web.SessionLocal() as db:
            _, actor = await web.identity(request, db)
            if not actor or actor.role != "admin":
                return web.forbidden_page(request)
        return web.render_page(request, "Импорт заявок", web.templates.get_template("request_import.html").render(csrf=web.csrf_token(request)))

    @app.post("/admin/import/preview")
    async def import_preview(request: Request):
        form = await web.read_form(request)
        if not web.verify_csrf(request, form):
            return web.forbidden_page(request)
        async with web.SessionLocal() as db:
            _, actor = await web.identity(request, db)
            if not actor or actor.role != "admin":
                return web.forbidden_page(request)
            try:
                batch, rows, skipped = await preview_import(db, actor, form.get("payload", ""), form.get("format", ""))
            except ValueError as exc:
                return web.error_page(request, str(exc), "/admin/requests/import")
        return web.render_page(request, "Предпросмотр импорта", web.templates.get_template("request_import.html").render(csrf=web.csrf_token(request), batch=batch, rows=rows, skipped=skipped, statuses=STATUSES, priorities=PRIORITIES))

    @app.post("/admin/import/confirm")
    async def import_confirm(request: Request):
        form = await web.read_form(request)
        if not web.verify_csrf(request, form):
            return web.forbidden_page(request)
        async with web.SessionLocal() as db:
            _, actor = await web.identity(request, db)
            if not actor or actor.role != "admin":
                return web.forbidden_page(request)
            try:
                count = await confirm_import(db, actor, form.get("batch", ""))
            except ValueError as exc:
                return web.error_page(request, str(exc), "/admin/requests/import")
        return web.render_page(request, "Импорт завершён", f"<section class='card'><h1>Импорт завершён</h1><p>Добавлено заявок: {count}. AI и выдача сертификатов не запускались.</p><a class='button' href='/admin/requests'>К заявкам</a></section>")

    @app.get("/admin/ai")
    async def ai_access(request: Request):
        async with web.SessionLocal() as db:
            _, actor = await web.identity(request, db)
            if not actor or actor.role != "admin":
                return web.forbidden_page(request)
            users = (await db.scalars(select(User).order_by(User.full_name))).all()
            query = request.query_params.get("q", "").strip()[:100]
            rows = []
            for user in users:
                if query and query.casefold() not in f"{user.full_name} {user.phone} {user.telegram_id}".casefold():
                    continue
                wallet = await get_wallet(db, user)
                rows.append({"id": user.id, "name": user.full_name, "phone": user.phone, "role": user.role, "remaining": available_analyses(wallet), "reference": secrets.token_urlsafe(24)})
            await db.commit()
            grants = (await db.scalars(select(AiCreditGrant).order_by(AiCreditGrant.id.desc()).limit(50))).all()
        return web.render_page(request, "Доступ к AI", web.templates.get_template("ai_access.html").render(query=query, rows=rows, grants=grants, names={u.telegram_id: u.full_name for u in users}, csrf=web.csrf_token(request)))

    @app.post("/admin/ai/set")
    async def set_access(request: Request):
        form = await web.read_form(request)
        if not web.verify_csrf(request, form):
            return web.forbidden_page(request)
        async with web.SessionLocal() as db:
            _, actor = await web.identity(request, db)
            if not actor or actor.role != "admin":
                return web.forbidden_page(request)
            try:
                if not re.fullmatch(r"[0-9]{1,5}", form.get("count", "")) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", form.get("reference", "")):
                    raise ValueError("Укажите целое число попыток от 0 до 10000.")
                target = await db.get(User, int(form.get("user_id", "")))
                if target is None:
                    raise ValueError("Пользователь не найден.")
                await set_analysis_balance(db, actor, target, int(form["count"]), form["reference"])
            except ValueError as exc:
                return web.error_page(request, str(exc), "/admin/ai")
        return RedirectResponse("/admin/ai?saved=1", status_code=303)

    @app.post("/admin/ai/grant")
    async def grant_access(request: Request):
        form = await web.read_form(request)
        if not web.verify_csrf(request, form):
            return web.forbidden_page(request)
        async with web.SessionLocal() as db:
            _, actor = await web.identity(request, db)
            if not actor or actor.role != "admin":
                return web.forbidden_page(request)
            try:
                target = await db.get(User, int(form.get("user_id", "")))
                reference = form.get("reference", "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{32}", reference):
                    raise ValueError("Обновите страницу выдачи доступа.")
                if target is None or form.get("reason") not in {"payment", "support"}:
                    raise ValueError("Выберите пользователя и основание.")
                await grant_analysis(db, actor, target, int(form.get("count", "1")), support=form.get("reason") == "support", reference=reference)
            except ValueError as exc:
                return web.error_page(request, str(exc), "/admin/ai")
        return RedirectResponse("/admin/ai", status_code=303)

    @app.post("/requests/{request_id}/ai")
    async def run_ai(request: Request, request_id: int):
        form = await web.read_form(request)
        if not web.verify_csrf(request, form):
            return web.forbidden_page(request)
        async with web.SessionLocal() as db:
            _, actor = await web.identity(request, db)
            item = await get_repair_request(db, request_id)
            if not web.can_view_request(request, actor, item):
                return web.forbidden_page(request)
            file_id = request_photo_id(item)
            description, actor_id = item.description, actor.id
        photo = None
        if file_id:
            try:
                photo = await web.download_request_photo(file_id)
            except Exception:
                return web.error_page(request, "Фото временно недоступно. Повторите позже; попытка не списана.", f"/requests/{request_id}")
        await analyze_with_budget(web.SessionLocal, request_id, actor_id, web.analyze_repair_request, description, photo, file_id)
        return RedirectResponse(f"/requests/{request_id}", status_code=303)
