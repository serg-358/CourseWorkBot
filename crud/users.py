"""CRUD-операции для пользователей."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import User


async def get_user_by_telegram_id(db: AsyncSession, telegram_id: str) -> User | None:
    """Находит пользователя по Telegram ID."""
    result = await db.execute(select(User).where(User.telegram_id == str(telegram_id)))
    return result.scalar_one_or_none()


async def get_all_users(db: AsyncSession) -> list[User]:
    """Возвращает всех пользователей в порядке регистрации."""
    result = await db.execute(select(User).order_by(User.created_at, User.id))
    return list(result.scalars().all())


async def create_user(
    db: AsyncSession,
    telegram_id: str,
    full_name: str,
    phone: str,
    role: str = "client",
) -> User:
    """Создаёт пользователя и сохраняет его в базе данных."""
    user = User(
        telegram_id=str(telegram_id),
        full_name=full_name,
        phone=phone,
        role=role or "client",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def update_user(
    db: AsyncSession,
    user_id: int,
    *,
    full_name: str | None = None,
    phone: str | None = None,
    role: str | None = None,
) -> User | None:
    """Обновляет переданные поля пользователя; None означает, что записи нет."""
    user = await db.get(User, user_id)
    if user is None:
        return None
    if full_name is not None:
        user.full_name = full_name
    if phone is not None:
        user.phone = phone
    if role is not None:
        user.role = role
    await db.commit()
    await db.refresh(user)
    return user


async def delete_user(db: AsyncSession, user_id: int) -> bool:
    """Удаляет пользователя; возвращает False, если запись не найдена."""
    user = await db.get(User, user_id)
    if user is None:
        return False
    await db.delete(user)
    await db.commit()
    return True
