"""Закрытое хранилище фотографий сайта в общем volume бота и сайта."""

import asyncio
import re
import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from starlette.datastructures import UploadFile
from starlette.requests import Request

from repair_bot.database.session import DATABASE_PATH
from repair_bot.services.ai.diagnostics import prepare_ai_photo

MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_FORM_BYTES = MAX_PHOTO_BYTES + 256 * 1024
PHOTO_DIRECTORY = DATABASE_PATH.parent / "request_photos"
WEB_PHOTO = re.compile(r"web:([a-f0-9]{32}\.jpg)")


async def read_photo_form(request: Request) -> tuple[dict[str, str], bytes | None]:
    """Ограничивает весь поток до парсинга, закрывает временные файлы multipart."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_FORM_BYTES:
            raise ValueError("Фото должно быть не больше 5 МБ.")
        body.extend(chunk)
    delivered = False
    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": bytes(body), "more_body": False}
    parsed = Request(request.scope, receive=receive)
    async with parsed.form(max_files=1, max_fields=12, max_part_size=32 * 1024) as data:
        form = {key: value for key, value in data.items() if isinstance(value, str)}
        upload = data.get("photo")
        if not isinstance(upload, UploadFile) or not upload.filename:
            return form, None
        photo = await upload.read(MAX_PHOTO_BYTES + 1)
        if len(photo) > MAX_PHOTO_BYTES:
            raise ValueError("Фото должно быть не больше 5 МБ.")
        if not photo:
            raise ValueError("Выбран пустой файл. Загрузите фотографию.")
        return form, photo


def normalize_photo(data: bytes) -> bytes:
    """Проверяет реальные байты изображения, размер и удаляет EXIF/метаданные."""
    if len(data) > MAX_PHOTO_BYTES:
        raise ValueError("Фото должно быть не больше 5 МБ.")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"} or getattr(image, "is_animated", False):
                raise ValueError("Прикрепите обычное фото JPG, PNG или WebP.")
            if image.width * image.height > 20_000_000:
                raise ValueError("Разрешение фото должно быть не больше 20 мегапикселей.")
            image.verify()
        return prepare_ai_photo(data)
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        raise ValueError("Файл не является исправной фотографией JPG, PNG или WebP.") from None


def photo_path(reference: str) -> Path:
    """Имя всегда генерирует сервер; пути и расширения от пользователя не принимаем."""
    match = WEB_PHOTO.fullmatch(reference)
    if match is None:
        raise ValueError("Некорректная ссылка на фотографию.")
    return PHOTO_DIRECTORY / match.group(1)


async def save_web_photo(photo: bytes) -> str:
    """Сохраняет уже проверенный JPEG; ссылки доступны только через проверку прав."""
    reference = "web:" + uuid.uuid4().hex + ".jpg"
    path = photo_path(reference)
    def write():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as output:
            path.chmod(0o600)
            output.write(photo)
    await asyncio.to_thread(write)
    return reference


async def read_web_photo(reference: str) -> bytes:
    return await asyncio.to_thread(photo_path(reference).read_bytes)


async def discard_web_photo(reference: str) -> None:
    """Убирает файл, если сохранение заявки не состоялось."""
    await asyncio.to_thread(photo_path(reference).unlink, missing_ok=True)
