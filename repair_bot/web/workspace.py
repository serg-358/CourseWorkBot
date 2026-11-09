"""Представления рабочих разделов и сортировка заявок без изменения прав."""

import html
from datetime import date
from urllib.parse import urlencode

from repair_bot.database.models import RepairRequest, User, WebsiteAccount
from repair_bot.handlers.common import format_rubles
from repair_bot.services.pricing.catalog import CATALOG, calculate_cost
from repair_bot.services.pricing.request_total import final_total

STATUSES = {"new": "Новая", "in_progress": "В работе", "completed": "Завершена", "cancelled": "Отменена"}
PRIORITIES = {"low": "Низкий", "medium": "Средний", "high": "Высокий"}


def service_catalog() -> list[dict[str, str]]:
    """Использует клиентские цены действующего расчётного каталога."""
    descriptions = {
        "diagnostics": "Проверка симптомов и определение причины неисправности. Мастер уточнит необходимый объём ремонта.",
        "os_reinstall": "Переустановка операционной системы. Сохранение данных, лицензии и состав программ согласовываются отдельно.",
        "cooling_service": "Чистка от пыли и обслуживание системы охлаждения. Необходимость дополнительных работ определяет мастер.",
    }
    rows = []
    for code in ("diagnostics", "cooling_service", "os_reinstall", "ram_8gb", "ssd_500gb", "power_supply"):
        item = CATALOG[code]
        _items, _cost, price = calculate_cost([code])
        rows.append({"code": code, "title": item.title, "description": descriptions.get(code, "Расчётный ориентир стоимости компонента. Совместимость, наличие и работы по установке уточняет мастер."), "price": format_rubles(price), "category": "Работы" if code in descriptions else "Компоненты"})
    return rows


def request_price(item: RepairRequest) -> int | None:
    """Возвращает последний клиентский расчёт вместе с доплатой за срочность."""
    price = item.staff_edits[-1].client_price_kopecks if item.staff_edits else (
        item.ai_assessment.client_price_kopecks if item.ai_assessment else None
    )
    return final_total(item, price, item.urgency.surcharge_kopecks if item.urgency else 0) if price is not None else None


def sorted_requests(items: list[RepairRequest], params: dict[str, str]) -> list[RepairRequest]:
    """Фильтрует разрешённый список; отсутствующие цены всегда идут последними."""
    result = list(items)
    status = params.get("status", "")
    if status == "closed":
        result = [item for item in result if item.status in {"completed", "cancelled"}]
    elif status == "active":
        result = [item for item in result if item.status in {"new", "in_progress"}]
    elif status in STATUSES:
        result = [item for item in result if item.status == status]
    priority = params.get("priority", "")
    if priority in PRIORITIES:
        result = [item for item in result if item.priority == priority]
    for field, lower in (("from", True), ("to", False)):
        try:
            boundary = date.fromisoformat(params.get(field, ""))
        except ValueError:
            continue
        result = [item for item in result if (item.created_at.date() >= boundary if lower else item.created_at.date() <= boundary)]
    key = params.get("sort", "date")
    reverse = params.get("order", "desc") != "asc"
    known = [item for item in result if request_price(item) is not None] if key == "price" else result
    unknown = [item for item in result if request_price(item) is None] if key == "price" else []
    getters = {
        "id": lambda item: item.id,
        "date": lambda item: item.created_at,
        "status": lambda item: list(STATUSES).index(item.status) if item.status in STATUSES else 99,
        "priority": lambda item: {"low": 0, "medium": 1, "high": 2}.get(item.priority, 1),
        "price": request_price,
    }
    getter = getters.get(key, getters["date"])
    return sorted(known, key=lambda item: (getter(item), item.id), reverse=reverse) + sorted(unknown, key=lambda item: item.id, reverse=True)


def requests_table(items: list[RepairRequest], staff: bool = False, view: str = "", params: dict[str, str] | None = None, path: str = "/panel") -> str:
    """Строит рабочую таблицу с фильтрами, ценой и ссылками сортировки."""
    params = dict(params or {})
    params["tab"] = "requests"
    if view:
        params["view"] = view
    def url(**changes: str) -> str:
        return html.escape(path + "?" + urlencode({**params, **changes}), quote=True)
    def heading(label: str, field: str) -> str:
        current = params.get("sort", "date") == field
        ascending = params.get("order", "desc") == "asc"
        mark = (" ↑" if ascending else " ↓") if current else " ↕"
        return f"<th><a href='{url(sort=field, order='desc' if current and ascending else 'asc')}'>{label}{mark}</a></th>"
    def options(values: dict[str, str], selected: str) -> str:
        return "".join(f"<option value='{key}'{' selected' if selected == key else ''}>{label}</option>" for key, label in values.items())
    hidden = "<input type='hidden' name='tab' value='requests'>" + (f"<input type='hidden' name='view' value='{view}'>" if view else "")
    if params.get("trash") == "1":
        hidden += "<input type='hidden' name='trash' value='1'>"
    filters = (
        f"<form class='request-filters' method='get' action='{path}'>{hidden}"
        f"<label>Сортировать<select name='sort'>{options({'date':'По дате','id':'По номеру','status':'По статусу','priority':'По приоритету','price':'По цене'}, params.get('sort','date'))}</select></label>"
        f"<label>Порядок<select name='order'>{options({'desc':'По убыванию','asc':'По возрастанию'}, params.get('order','desc'))}</select></label>"
        f"<label>Статус<select name='status'>{options({'':'Все статусы', **STATUSES, 'active':'Активные', 'closed':'Архив'}, params.get('status',''))}</select></label>"
        f"<label>Приоритет<select name='priority'>{options({'':'Все приоритеты', **PRIORITIES}, params.get('priority',''))}</select></label>"
        f"<label>Дата от<input type='date' name='from' value='{html.escape(params.get('from',''), quote=True)}'></label>"
        f"<label>Дата до<input type='date' name='to' value='{html.escape(params.get('to',''), quote=True)}'></label>"
        f"<button>Применить</button><a href='{html.escape(path + '?' + urlencode({'tab':'requests', **({'view':view} if view else {})}), quote=True)}'>Сбросить</a></form>"
    )
    filtered = sorted_requests(items, params)
    rows = []
    for item in filtered:
        detail = f"/requests/{item.id}" + (f"?view={view}" if view else "")
        owner = f"<td>{html.escape(item.user.full_name)}</td>" if staff else ""
        price = request_price(item)
        price_text = format_rubles(price) if price is not None else "Уточняется"
        rows.append(f"<tr><td><a href='{detail}'>№{item.id}</a></td>{owner}<td><a href='{detail}'>{html.escape(item.description[:100])}</a></td><td>{STATUSES.get(item.status, 'Неизвестен')}</td><td>{PRIORITIES.get(item.priority, 'Средний')}</td><td>{item.created_at:%d.%m.%Y}</td><td class='price-cell'>{price_text}</td><td><a href='{detail}'>Открыть →</a></td></tr>")
    headers = heading("Номер", "id") + ("<th>Клиент</th>" if staff else "") + "<th>Описание</th>" + heading("Статус", "status") + heading("Приоритет", "priority") + heading("Создана", "date") + heading("Предварительный итог", "price") + "<th></th>"
    table = f"<div class='table-wrap'><table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>" if rows else "<p class='card'>По выбранным условиям заявок нет.</p>"
    return filters + f"<p class='muted'>Найдено: {len(filtered)} · Цена включает выбранную доплату за срочность.</p>" + table


def profile_content(account: WebsiteAccount, user: User | None) -> str:
    """Показывает только профиль текущего аккаунта."""
    values = [("Имя", user.full_name if user else "Не привязан"), ("Телефон", user.phone if user else "Не указан"), ("Email", account.email), ("Telegram", "Привязан" if user else "Ожидает привязки")]
    rows = "".join(f"<div><dt>{label}</dt><dd>{html.escape(value)}</dd></div>" for label, value in values)
    return f"<section class='card'><h1>Мой профиль</h1><dl class='profile-fields'>{rows}</dl><a class='button secondary' href='/bot'>Открыть Telegram-бота ↗</a></section>"


def feedback_content(csrf: str) -> str:
    """Выводит форму отправки сообщения администраторам."""
    return ("<section class='card feedback-card'><h1>Обратная связь</h1><p>Задайте вопрос о работе сервиса или сообщите о проблеме. Сообщение получат администраторы в Telegram.</p>"
            f"<form method='post' action='/feedback'><input type='hidden' name='csrf' value='{csrf}'>"
            "<label>Ваше сообщение<textarea name='message' minlength='10' maxlength='3000' required placeholder='Напишите, с чем нужна помощь…'></textarea></label><button>Отправить сообщение</button></form></section>")
