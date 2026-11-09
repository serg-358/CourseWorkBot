"""Общий шлюз AI для сайта и Telegram: без квоты провайдер не вызывается."""

import logging
from repair_bot.database.crud.ai_access import reserve_analysis, finish_analysis
from repair_bot.database.crud.requests import save_ai_assessment
from repair_bot.services.pricing.catalog import calculate_cost

logger = logging.getLogger(__name__)


async def analyze_with_budget(factory, request_id: int, actor_id: int, analyzer, description: str, photo_bytes=None, photo_file_id=None) -> str:
    """Резервирует попытку до внешнего вызова, сохраняет результат отдельно."""
    async with factory() as db:
        kind, attempt = await reserve_analysis(db, request_id, actor_id)
    if kind not in {"free", "paid", "staff"}:
        return kind
    state = "failed"
    try:
        result = await analyzer(description, photo_bytes)
        if result is None:
            state = "unavailable"
        else:
            items, cost, price = calculate_cost(result.catalog_item_codes)
            async with factory() as db:
                await save_ai_assessment(db, request_id, priority=result.priority, photo_file_id=photo_file_id,
                    client_causes=result.client_causes, client_recommendations=result.client_recommendations,
                    safety_note=result.safety_note, master_diagnosis=result.master_diagnosis,
                    parts=result.parts, tools=result.tools, catalog_item_codes=",".join(i.code for i in items) if cost is not None else "",
                    estimated_cost_kopecks=cost, client_price_kopecks=price)
            state = "done"
    except Exception:
        # Не записываем тело ответа провайдера, ключи или персональные данные.
        logger.warning("AI attempt failed for request %s", request_id)
    finally:
        async with factory() as db:
            await finish_analysis(db, request_id, attempt, state)
    return state


def ai_access_notice(item) -> str:
    """Понятное объяснение отсутствующей оценки без лишнего сообщения."""
    usage = getattr(item, "ai_access", None)
    if usage and usage.state == "reserved":
        return "AI-диагностика выполняется. Обновите карточку через несколько секунд."
    if usage and usage.kind == "payment_required":
        return "Бесплатная AI-попытка уже использована, доступных попыток нет. Онлайн-оплата пока не подключена. Дополнительный доступ выдаёт администратор. Заявка сохранена: мастер рассмотрит её без AI."
    if usage and usage.state == "failed":
        return "AI не ответил. Заявка сохранена; обратитесь к администратору для служебной повторной попытки."
    return "Предварительная AI-оценка сейчас недоступна; заявку рассмотрит мастер."
