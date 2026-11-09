"""Версионированный перенос заявок: проверка до записи, CSV безопасен для Excel."""

import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone

MAX_BYTES = 512 * 1024
MAX_ROWS = 200
FIELDS = ("source_id", "telegram_id", "full_name", "phone", "description", "status", "priority", "created_at", "updated_at", "diagnosis", "address", "urgency", "ai", "edits", "photo_file_id", "deleted_at")
NESTED = {"urgency", "ai", "edits"}
AI_TEXT = ("client_causes", "client_recommendations", "safety_note", "master_diagnosis", "parts", "tools", "catalog_item_codes")


def identity_key(row: dict) -> str:
    """Стабильная идентичность исходной заявки при повторном импорте."""
    return hashlib.sha256(f"{row['telegram_id']}|{row['source_id']}|{row['created_at']}".encode()).hexdigest()


def export_row(item, address: str = "") -> dict:
    """Сохраняет исходную AI-оценку и историю правок без паролей и ролей."""
    from repair_bot.database.crud.request_lifecycle import request_photo_id
    a = item.ai_assessment
    row = dict(source_id=item.id, telegram_id=item.user.telegram_id, full_name=item.user.full_name,
        phone=item.user.phone, description=item.description, status=item.status, priority=item.priority,
        created_at=item.created_at.isoformat(), updated_at=item.updated_at.isoformat(), diagnosis=item.diagnosis or "",
        address=address, photo_file_id=request_photo_id(item) or "",
        deleted_at=item.lifecycle.deleted_at.isoformat() if item.lifecycle and item.lifecycle.deleted_at else "",
        urgency={"tariff_code": item.urgency.tariff_code, "label": item.urgency.label, "surcharge_kopecks": item.urgency.surcharge_kopecks} if item.urgency else None,
        ai={**{k: getattr(a, k) for k in AI_TEXT}, "estimated_cost_kopecks": a.estimated_cost_kopecks, "client_price_kopecks": a.client_price_kopecks} if a else None,
        edits=[{"editor_name": e.editor_name, "diagnosis": e.diagnosis, "client_price_kopecks": e.client_price_kopecks,
            "created_at": e.created_at.isoformat(), "parts": e.details.parts if e.details else None, "tools": e.details.tools if e.details else None} for e in item.staff_edits])
    # Повторный экспорт импортированной записи сохраняет исходную идентичность.
    if item.lifecycle and item.lifecycle.provenance:
        original = json.loads(item.lifecycle.provenance)
        row["source_id"] = original["source_id"]
        row["created_at"] = original["created_at"]
    return row


def serialize_rows(rows: list[dict], kind: str) -> str:
    """JSON — структурированный архив; CSV — UTF-8 с BOM и разделителем ;."""
    if kind == "json":
        return json.dumps({"version": 1, "requests": rows}, ensure_ascii=False, indent=2)
    if kind != "csv":
        raise ValueError("Выберите JSON или CSV.")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, delimiter=";")
    writer.writeheader()
    for row in rows:
        values = {k: json.dumps(row[k], ensure_ascii=False) if k in NESTED else str(row.get(k, "")) for k in FIELDS}
        writer.writerow({k: "'" + v if v.startswith(("'", "=", "+", "-", "@", "\t", "\r", "\n")) else v for k, v in values.items()})
    return "\ufeff" + output.getvalue()


def _text(value, label: str, limit: int, minimum: int = 0) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= limit or "\x00" in value:
        raise ValueError(f"Некорректное поле «{label}».")
    return value


def _price(value, nullable: bool = False):
    if value is None and nullable:
        return None
    if type(value) is not int or not 0 <= value <= 1_000_000_000:
        raise ValueError("Цена должна быть целым числом копеек от 0 до 1 млрд.")
    return value


def _date(value: str) -> str:
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo:
            date = date.astimezone(timezone.utc).replace(tzinfo=None)
        return date.isoformat()
    except (TypeError, ValueError):
        raise ValueError("Некорректная дата ISO 8601.") from None


def parse_transfer(payload: str, kind: str) -> list[dict]:
    """Полностью проверяет архив до появления каких-либо новых пользователей."""
    if len(payload.encode()) > MAX_BYTES:
        raise ValueError("Файл превышает 512 КиБ. Разделите архив.")
    try:
        if kind == "json":
            document = json.loads(payload.lstrip("\ufeff"))
            if not isinstance(document, dict) or document.get("version") != 1:
                raise ValueError("Требуется JSON-архив версии 1.")
            rows = document.get("requests")
        elif kind == "csv":
            reader = csv.DictReader(io.StringIO(payload.lstrip("\ufeff")), delimiter=";")
            if set(reader.fieldnames or []) != set(FIELDS):
                raise ValueError("Заголовки CSV должны соответствовать экспорту сайта.")
            rows = []
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError("Повреждена строка CSV.")
                row = {k: v[1:] if v.startswith("'") and v[1:].startswith(("'", "=", "+", "-", "@", "\t", "\r", "\n")) else v for k, v in row.items()}
                for key in NESTED:
                    row[key] = json.loads(row[key])
                row["source_id"] = int(row["source_id"])
                rows.append(row)
        else:
            raise ValueError("Выберите JSON или CSV.")
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
            raise ValueError("Архив должен содержать от 1 до 200 заявок.")
        return [_validate_row(row) for row in rows]
    except (json.JSONDecodeError, csv.Error, KeyError, TypeError, OverflowError, RecursionError):
        raise ValueError("Файл не соответствует формату экспорта. Записи не изменены.") from None


def _validate_row(row: dict) -> dict:
    if not isinstance(row, dict) or type(row.get("source_id")) is not int or row["source_id"] < 1:
        raise ValueError("Неверный исходный номер заявки.")
    result = {"source_id": row["source_id"]}
    for key, limit, minimum in (("telegram_id", 20, 1), ("full_name", 200, 3), ("phone", 20, 1), ("description", 20000, 1), ("diagnosis", 10000, 0), ("address", 500, 0), ("photo_file_id", 500, 0)):
        result[key] = _text(row.get(key, ""), key, limit, minimum)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", result["telegram_id"]) or not re.fullmatch(r"\+?[0-9]{5,19}", result["phone"]):
        raise ValueError("Неверный Telegram ID или телефон владельца.")
    for key, choices in (("status", {"new", "in_progress", "completed", "cancelled"}), ("priority", {"low", "medium", "high"})):
        if row.get(key) not in choices:
            raise ValueError(f"Неизвестное значение {key}.")
        result[key] = row[key]
    for key in ("created_at", "updated_at"):
        result[key] = _date(row.get(key))
    result["deleted_at"] = _date(row["deleted_at"]) if row.get("deleted_at") else ""
    urgency = row.get("urgency")
    result["urgency"] = None if urgency is None else {"tariff_code": _text(urgency["tariff_code"], "tariff", 32, 1), "label": _text(urgency["label"], "label", 100, 1), "surcharge_kopecks": _price(urgency["surcharge_kopecks"])}
    ai = row.get("ai")
    result["ai"] = None if ai is None else {**{k: _text(ai[k], k, 10000) for k in AI_TEXT}, **{k: _price(ai.get(k), True) for k in ("estimated_cost_kopecks", "client_price_kopecks")}}
    edits = row.get("edits", [])
    if not isinstance(edits, list) or len(edits) > 100:
        raise ValueError("Некорректная история правок.")
    result["edits"] = [{"editor_name": _text(e["editor_name"], "author", 200, 1), "diagnosis": _text(e["diagnosis"], "diagnosis", 3000), "client_price_kopecks": _price(e["client_price_kopecks"]), "created_at": _date(e["created_at"]), **{k: None if e.get(k) is None else _text(e[k], k, 3000) for k in ("parts", "tools")}} for e in edits]
    return result
