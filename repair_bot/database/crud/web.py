"""Операции базы для веб-аккаунтов и привязки к Telegram."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repair_bot.database.models import (
    TelegramLinkCode,
    User,
    WebsiteAccount,
    utcnow,
)


async def get_website_account_by_email(
    db: AsyncSession, email: str
) -> WebsiteAccount | None:
    """Ищет веб-аккаунт по нормализованному email."""
    result = await db.execute(
        select(WebsiteAccount).where(WebsiteAccount.email == email.lower().strip())
    )
    return result.scalar_one_or_none()


async def get_website_account(
    db: AsyncSession, account_id: int
) -> WebsiteAccount | None:
    """Возвращает веб-аккаунт вместе со связанным Telegram-пользователем."""
    result = await db.execute(
        select(WebsiteAccount)
        .where(WebsiteAccount.id == account_id)
        .execution_options(populate_existing=True)
    )
    account = result.scalar_one_or_none()
    if account is not None and account.user_id is not None:
        await db.refresh(account, attribute_names=["user"])
    return account


async def create_website_account(
    db: AsyncSession, email: str, password_hash: str
) -> WebsiteAccount:
    """Создает еще не связанный с Telegram клиентский веб-аккаунт."""
    account = WebsiteAccount(email=email.lower().strip(), password_hash=password_hash)
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


async def save_link_code(
    db: AsyncSession,
    account_id: int,
    code_hash: str,
    expires_at: datetime,
) -> TelegramLinkCode:
    """Сохраняет новый одноразовый код, аннулируя предыдущие неиспользованные."""
    result = await db.execute(
        select(TelegramLinkCode).where(
            TelegramLinkCode.account_id == account_id,
            TelegramLinkCode.used_at.is_(None),
        )
    )
    for old_code in result.scalars():
        old_code.used_at = utcnow()
    link_code = TelegramLinkCode(
        account_id=account_id,
        code_hash=code_hash,
        expires_at=expires_at,
    )
    db.add(link_code)
    await db.commit()
    await db.refresh(link_code)
    return link_code


async def link_telegram_account(
    db: AsyncSession, telegram_id: str, code_hash: str
) -> str:
    """Привязывает Telegram-пользователя активным кодом и возвращает результат."""
    now = utcnow()
    result = await db.execute(
        select(TelegramLinkCode)
        .where(
            TelegramLinkCode.code_hash == code_hash,
            TelegramLinkCode.used_at.is_(None),
            TelegramLinkCode.expires_at > now,
        )
        .with_for_update()
    )
    link_code = result.scalar_one_or_none()
    if link_code is None:
        return "invalid"

    result = await db.execute(select(User).where(User.telegram_id == str(telegram_id)))
    user = result.scalar_one_or_none()
    if user is None:
        return "telegram_user_missing"
    await db.refresh(link_code, attribute_names=["account"])
    account = link_code.account
    if account.user_id is not None:
        return "account_already_linked"

    result = await db.execute(
        select(WebsiteAccount).where(WebsiteAccount.user_id == user.id)
    )
    if result.scalar_one_or_none() is not None:
        return "telegram_already_linked"

    account.user_id = user.id
    link_code.used_at = now
    await db.commit()
    return "linked"
