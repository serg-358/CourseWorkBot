"""Выдача кода в Telegram и атомарная привязка на сайте."""

import hashlib
import hmac
import os
import re
import secrets
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from repair_bot.database.models import LinkCodeRateLimit, TelegramAccessCode, User, WebsiteAccount, utcnow

TTL = timedelta(minutes=10)


def code_digest(code: str) -> str:
    """Не позволяет перебрать короткие коды по одной лишь копии базы."""
    secret = os.getenv("WEB_SESSION_SECRET") or os.getenv("BOT_TOKEN")
    if not secret:
        raise RuntimeError("Не настроен секрет одноразовых кодов.")
    return hmac.new(secret.encode(), ("telegram-access:" + code).encode(), hashlib.sha256).hexdigest()


async def issue_telegram_code(db: AsyncSession, telegram_id: str) -> tuple[str, str | None]:
    """Выдаёт новый код зарегистрированному владельцу личного Telegram-чата."""
    user = await db.scalar(select(User).where(User.telegram_id == str(telegram_id)).with_for_update())
    if user is None:
        return "not_registered", None
    if await db.scalar(select(WebsiteAccount.id).where(WebsiteAccount.user_id == user.id)):
        return "already_linked", None
    now = utcnow()
    latest = await db.scalar(select(TelegramAccessCode).where(TelegramAccessCode.user_id == user.id).order_by(TelegramAccessCode.created_at.desc()).limit(1))
    if latest and latest.created_at > now - timedelta(seconds=60):
        return "cooldown", None
    await db.execute(delete(TelegramAccessCode).where(TelegramAccessCode.expires_at <= now))
    await db.execute(update(TelegramAccessCode).where(TelegramAccessCode.user_id == user.id, TelegramAccessCode.used_at.is_(None)).values(used_at=now))
    for _ in range(5):
        code = f"{secrets.randbelow(100_000_000):08d}"
        if not await db.scalar(select(TelegramAccessCode.id).where(TelegramAccessCode.code_hash == code_digest(code))):
            break
    else:
        await db.rollback()
        return "busy", None
    db.add(TelegramAccessCode(user_id=user.id, code_hash=code_digest(code), expires_at=now + TTL))
    await db.commit()
    return "issued", code


async def _take_attempt(db: AsyncSession, key: str, limit: int) -> bool:
    now = utcnow()
    if await db.get(LinkCodeRateLimit, key) is None:
        try:
            async with db.begin_nested():
                db.add(LinkCodeRateLimit(key=key, window_start=now, attempts=0))
                await db.flush()
        except IntegrityError:
            pass
    await db.execute(update(LinkCodeRateLimit).where(LinkCodeRateLimit.key == key, LinkCodeRateLimit.window_start <= now - TTL).values(window_start=now, attempts=0))
    claimed = await db.scalar(update(LinkCodeRateLimit).where(LinkCodeRateLimit.key == key, LinkCodeRateLimit.attempts < limit).values(attempts=LinkCodeRateLimit.attempts + 1).returning(LinkCodeRateLimit.key))
    return claimed is not None


async def verify_telegram_code(db: AsyncSession, account_id: int, code: str, ip: str) -> str:
    """Проверяет лимиты, одноразовость и обе стороны связи в одной транзакции."""
    account = await db.get(WebsiteAccount, account_id)
    if account is None:
        return "invalid"
    if account.user_id is not None:
        return "account_already_linked"
    code = code.strip()
    if not re.fullmatch(r"[0-9]{8}", code):
        return "invalid_format"
    # IP не сохраняется в открытом виде. Лимиты переживают logout и перезапуск.
    ip_key = "ip:" + code_digest("rate:" + ip)
    for key, limit in ((f"account:{account_id}", 5), (ip_key, 20)):
        if not await _take_attempt(db, key, limit):
            await db.commit()
            return "rate_limited"
    now = utcnow()
    record = await db.scalar(select(TelegramAccessCode).where(TelegramAccessCode.code_hash == code_digest(code), TelegramAccessCode.used_at.is_(None), TelegramAccessCode.expires_at > now).with_for_update())
    if record is None:
        await db.commit()
        return "invalid"
    if await db.scalar(select(WebsiteAccount.id).where(WebsiteAccount.user_id == record.user_id)):
        await db.commit()
        return "telegram_already_linked"
    try:
        async with db.begin_nested():
            used = await db.execute(update(TelegramAccessCode).where(TelegramAccessCode.id == record.id, TelegramAccessCode.used_at.is_(None), TelegramAccessCode.expires_at > now).values(used_at=now))
            linked = await db.execute(update(WebsiteAccount).where(WebsiteAccount.id == account_id, WebsiteAccount.user_id.is_(None)).values(user_id=record.user_id))
            if used.rowcount != 1 or linked.rowcount != 1:
                raise ValueError("Code already consumed")
    except (IntegrityError, ValueError):
        await db.commit()
        return "invalid"
    # Сохраняем лимиты и связь; удаляем просроченные записи без раскрытия кодов.
    await db.execute(delete(LinkCodeRateLimit).where(LinkCodeRateLimit.window_start < now - timedelta(days=1)))
    await db.commit()
    return "linked"
