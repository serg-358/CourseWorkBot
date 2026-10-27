"""Изолированная интеграция OpenAI для предварительной диагностики."""

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

from pricing_catalog import CATALOG

AI_TIMEOUT_SECONDS = 20
AI_IMAGE_MAX_SIDE = 1600
AI_IMAGE_MAX_BYTES = 1024 * 1024
AI_TEMPERATURE = 0
VALID_PRIORITIES = {"low", "medium", "high"}
EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
PHONE_PATTERN = re.compile(
    r"(?<!\w)(?:\+7|8)[\s()\-]*\d(?:[\s()\-]*\d){9}(?!\w)"
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AiResult:
    priority: str
    client_causes: str
    client_recommendations: str
    safety_note: str
    master_diagnosis: str
    parts: str
    tools: str
    catalog_item_codes: list[str]


_SCHEMA = {
    "type": "object",
    "properties": {
        "priority": {"type": "string", "enum": ["low", "medium", "high"]},
        "client_causes": {"type": "string"},
        "client_recommendations": {"type": "string"},
        "safety_note": {"type": "string"},
        "master_diagnosis": {"type": "string"},
        "parts": {"type": "string"},
        "tools": {"type": "string"},
        "catalog_item_codes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "priority", "client_causes", "client_recommendations", "safety_note",
        "master_diagnosis", "parts", "tools", "catalog_item_codes",
    ],
    "additionalProperties": False,
}


def sanitize_description(description: str) -> str:
    """Маскирует контакты, которые не нужны для предварительной диагностики."""
    description = EMAIL_PATTERN.sub("[email скрыт]", description)
    return PHONE_PATTERN.sub("[телефон скрыт]", description)


def prepare_ai_photo(photo_bytes: bytes) -> bytes:
    """Удаляет метаданные и сжимает фото для безопасной отправки в AI."""
    with Image.open(BytesIO(photo_bytes)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((AI_IMAGE_MAX_SIDE, AI_IMAGE_MAX_SIDE))
        quality = 85
        while quality >= 40:
            result = BytesIO()
            image.save(result, format="JPEG", quality=quality, optimize=True)
            if result.tell() <= AI_IMAGE_MAX_BYTES or quality == 40:
                return result.getvalue()
            quality -= 15
    raise ValueError("Не удалось подготовить фото для AI")


def normalize_catalog_item_codes(codes: list[object]) -> list[str]:
    """Оставляет допустимые позиции и гарантирует стоимость диагностики."""
    valid_codes = [code for code in codes if isinstance(code, str) and code in CATALOG]
    return list(dict.fromkeys(["diagnostics", *valid_codes]))


def is_unsupported_temperature_error(error: Exception) -> bool:
    """Определяет ответ OpenAI-совместимого шлюза о неподдержанном параметре."""
    message = str(error).lower()
    return "temperature" in message and any(
        marker in message
        for marker in ("unsupported", "not supported", "unknown parameter", "invalid parameter")
    )


async def analyze_repair_request(description: str, photo_bytes: bytes | None = None) -> AiResult | None:
    """Анализирует обезличенное описание и необязательное фото; ошибки отдаёт вызывающему коду."""
    api_key = os.getenv("KODIKROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, BadRequestError

    description = sanitize_description(description)
    catalog = "; ".join(f"{item.code}: {item.title}" for item in CATALOG.values())
    prompt = (
        "Ты помощник мастера по ремонту ПК. Дай предварительную, не гарантированную оценку "
        "на русском. Не советуй опасные действия, работу под напряжением или разборку без мастера. "
        "Для стабильной предварительной цены всегда включай код diagnostics. Добавляй дорогую "
        "деталь или работу только при прямом признаке в описании либо на фото; при неуверенности "
        "оставляй только diagnostics. Не выводи hardware-код лишь из общего симптома «не включается». "
        "Коды os_reinstall допустимы при явной программной проблеме, ram_8gb — при ошибке памяти "
        "или прямо названной нехватке ОЗУ, ssd_500gb — при ошибке накопителя или SMART, power_supply "
        "— при прямо указанной неисправности питания, cooling_service — при явном перегреве, шуме "
        f"или загрязнении. Выбирай коды только из каталога: {catalog}. Описание: {description}"
    )
    content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
    if photo_bytes:
        encoded = base64.b64encode(prepare_ai_photo(photo_bytes)).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{encoded}",
                    "detail": "high",
                },
            }
        )
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=os.getenv("KODIKROUTER_BASE_URL", "https://api.kodikrouter.ru/v1"),
        max_retries=0,
    )
    try:
        async def create_completion(*, include_temperature: bool):
            request_args: dict[str, object] = {
                "model": os.getenv("KODIKROUTER_MODEL", "openai/gpt-5.4-mini"),
                "messages": [{"role": "user", "content": content}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "repair_assessment",
                        "strict": True,
                        "schema": _SCHEMA,
                    },
                },
            }
            if include_temperature:
                request_args["temperature"] = AI_TEMPERATURE
            for attempt in range(2):
                try:
                    return await asyncio.wait_for(
                        client.chat.completions.create(**request_args),
                        timeout=AI_TIMEOUT_SECONDS,
                    )
                except (asyncio.TimeoutError, APIConnectionError, APITimeoutError):
                    if attempt:
                        raise
            raise RuntimeError("AI request did not finish")

        try:
            response = await create_completion(include_temperature=True)
        except BadRequestError as error:
            if not is_unsupported_temperature_error(error):
                raise
            logger.warning("AI-шлюз не поддерживает temperature; повторяем запрос без параметра.")
            response = await create_completion(include_temperature=False)
    finally:
        await client.close()
    response_text = response.choices[0].message.content
    if not response_text:
        raise ValueError("AI returned an empty assessment")
    data = json.loads(response_text)
    codes = data["catalog_item_codes"]
    if data["priority"] not in VALID_PRIORITIES or not isinstance(codes, list):
        raise ValueError("AI returned an invalid assessment")
    data["catalog_item_codes"] = normalize_catalog_item_codes(codes)
    return AiResult(**data)
