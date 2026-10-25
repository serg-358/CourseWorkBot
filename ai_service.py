"""Изолированная интеграция OpenAI для предварительной диагностики."""

import asyncio
import base64
import json
import os
import re
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

from pricing_catalog import CATALOG

AI_TIMEOUT_SECONDS = 20
AI_IMAGE_MAX_SIDE = 1600
AI_IMAGE_MAX_BYTES = 1024 * 1024
VALID_PRIORITIES = {"low", "medium", "high"}
EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
PHONE_PATTERN = re.compile(
    r"(?<!\w)(?:\+7|8)[\s()\-]*\d(?:[\s()\-]*\d){9}(?!\w)"
)


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


async def analyze_repair_request(description: str, photo_bytes: bytes | None = None) -> AiResult | None:
    """Анализирует обезличенное описание и необязательное фото; ошибки отдаёт вызывающему коду."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

    description = sanitize_description(description)
    catalog = "; ".join(f"{item.code}: {item.title}" for item in CATALOG.values())
    prompt = (
        "Ты помощник мастера по ремонту ПК. Дай предварительную, не гарантированную оценку "
        "на русском. Не советуй опасные действия, работу под напряжением или разборку без мастера. "
        f"Выбирай коды только из каталога: {catalog}. Описание: {description}"
    )
    content: list[dict[str, str]] = [{"type": "input_text", "text": prompt}]
    if photo_bytes:
        encoded = base64.b64encode(prepare_ai_photo(photo_bytes)).decode("ascii")
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}", "detail": "low"})
    client = AsyncOpenAI(api_key=api_key, max_retries=0)
    try:
        for attempt in range(2):
            try:
                response = await asyncio.wait_for(
                    client.responses.create(
                        model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
                        input=[{"role": "user", "content": content}],
                        text={"format": {"type": "json_schema", "name": "repair_assessment", "strict": True, "schema": _SCHEMA}},
                    ),
                    timeout=AI_TIMEOUT_SECONDS,
                )
                break
            except (asyncio.TimeoutError, APIConnectionError, APITimeoutError):
                if attempt:
                    raise
        else:
            raise RuntimeError("AI request did not finish")
    finally:
        await client.close()
    data = json.loads(response.output_text)
    codes = data["catalog_item_codes"]
    if data["priority"] not in VALID_PRIORITIES or not isinstance(codes, list):
        raise ValueError("AI returned an invalid assessment")
    return AiResult(**data)
