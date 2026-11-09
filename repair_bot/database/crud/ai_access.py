"""Атомарный учёт бесплатного и оплаченного доступа к AI."""

from datetime import timedelta
from sqlalchemy import select, update, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from repair_bot.database.models import AiWallet, AiUsage, AiCreditGrant, RepairRequest, RepairRequestAiAssessment, User, utcnow

AI_PRICE_KOPECKS = 3000


def available_analyses(wallet: AiWallet) -> int:
    """Единый видимый остаток, включая ещё не использованную бесплатную попытку."""
    return wallet.credits + int(not wallet.free_used)


async def set_analysis_balance(db: AsyncSession, actor: User, target: User, count: int, reference: str) -> None:
    """Устанавливает точный остаток, а не добавляет к нему скрытую бесплатную попытку."""
    if actor.role != "admin" or not 0 <= count <= 10000:
        raise ValueError("Укажите целое количество от 0 до 10000.")
    if await db.scalar(select(AiCreditGrant.id).where(AiCreditGrant.reference == reference)):
        return
    await get_wallet(db, target)
    try:
        db.add(AiCreditGrant(telegram_id=target.telegram_id, actor_name=actor.full_name, credits=count, amount_kopecks=0, reason="balance_set", reference=reference))
        await db.flush()
        await db.execute(update(AiWallet).where(AiWallet.telegram_id == target.telegram_id).values(credits=count, free_used=True))
        # Завершение старой незапущенной/неудачной попытки не увеличит новый остаток.
        for kind in ("free", "paid"):
            await db.execute(update(AiUsage).where(AiUsage.actor_telegram_id == target.telegram_id, AiUsage.state == "reserved", AiUsage.kind == kind).values(kind="adjusted_" + kind))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if not await db.scalar(select(AiCreditGrant.id).where(AiCreditGrant.reference == reference)):
            raise


async def get_wallet(db: AsyncSession, user: User) -> AiWallet:
    """Старые успешные диагностики засчитываются в бесплатный лимит."""
    wallet = await db.get(AiWallet, user.telegram_id)
    if wallet is None:
        historical = await db.scalar(select(RepairRequestAiAssessment.id).join(RepairRequest).where(RepairRequest.user_id == user.id).limit(1))
        try:
            async with db.begin_nested():
                wallet = AiWallet(telegram_id=user.telegram_id, free_used=historical is not None, credits=0)
                db.add(wallet)
                await db.flush()
        except IntegrityError:
            wallet = await db.get(AiWallet, user.telegram_id)
    return wallet


async def reserve_analysis(db: AsyncSession, request_id: int, actor_id: int) -> tuple[str, int]:
    """Одновременно допускает только одну попытку на заявку; роль берёт из БД."""
    from repair_bot.database.crud.requests import get_repair_request
    actor = await db.get(User, actor_id)
    item = await get_repair_request(db, request_id)
    if not actor or not item or (actor.role not in {"admin", "master"} and item.user_id != actor.id):
        return "forbidden", 0
    if item.ai_assessment or (item.lifecycle and item.lifecycle.imported):
        return "already_done", 0
    if item.status not in {"new", "in_progress"}:
        return "closed", 0
    usage = await db.get(AiUsage, request_id)
    if usage is None:
        try:
            async with db.begin_nested():
                usage = AiUsage(request_id=request_id, actor_telegram_id=actor.telegram_id, state="blocked", attempts=0)
                db.add(usage)
                await db.flush()
        except IntegrityError:
            usage = await db.get(AiUsage, request_id)
    # После аварийного завершения процесса старая попытка не блокирует заявку навсегда.
    # Её расход не возвращаем автоматически: это устранило бы защиту от повторов.
    attempt = await db.scalar(update(AiUsage).where(AiUsage.request_id == request_id, or_(AiUsage.state != "reserved", AiUsage.updated_at < utcnow() - timedelta(minutes=5)), AiUsage.state != "done").values(state="reserved", attempts=AiUsage.attempts + 1, actor_telegram_id=actor.telegram_id, updated_at=utcnow()).returning(AiUsage.attempts))
    if attempt is None:
        await db.commit()
        return "busy", 0
    kind = "staff"
    if actor.role not in {"admin", "master"}:
        await get_wallet(db, actor)
        free = await db.scalar(update(AiWallet).where(AiWallet.telegram_id == actor.telegram_id, AiWallet.free_used.is_(False)).values(free_used=True).returning(AiWallet.telegram_id))
        if free:
            kind = "free"
        else:
            paid = await db.scalar(update(AiWallet).where(AiWallet.telegram_id == actor.telegram_id, AiWallet.credits > 0).values(credits=AiWallet.credits - 1).returning(AiWallet.telegram_id))
            kind = "paid" if paid else "payment_required"
    await db.execute(update(AiUsage).where(AiUsage.request_id == request_id).values(kind=kind, state="blocked" if kind == "payment_required" else "reserved"))
    await db.commit()
    return kind, attempt


async def finish_analysis(db: AsyncSession, request_id: int, attempt: int, state: str) -> None:
    """Ошибка провайдера расходует попытку; отсутствие настройки — возвращает её."""
    row = await db.get(AiUsage, request_id)
    if row is None or row.state != "reserved" or row.attempts != attempt:
        return
    if state == "unavailable" and row.kind in {"free", "paid"}:
        values = {"free_used": False} if row.kind == "free" else {"credits": AiWallet.credits + 1}
        await db.execute(update(AiWallet).where(AiWallet.telegram_id == row.actor_telegram_id).values(**values))
    row.state = state
    row.updated_at = utcnow()
    await db.commit()


async def grant_analysis(db: AsyncSession, actor: User, target: User, count: int, *, support: bool = False, reference: str | None = None) -> None:
    """Администратор подтверждает оплату; служебная компенсация выделена в аудите."""
    if actor.role != "admin" or not 1 <= count <= 100:
        raise ValueError("Недопустимая выдача AI-доступа.")
    if reference and await db.scalar(select(AiCreditGrant.id).where(AiCreditGrant.reference == reference)):
        return
    await get_wallet(db, target)
    try:
        # Уникальный reference защищает от двойного нажатия/повторного POST.
        db.add(AiCreditGrant(telegram_id=target.telegram_id, actor_name=actor.full_name, credits=count, amount_kopecks=0 if support else count * AI_PRICE_KOPECKS, reason="support" if support else "payment_verified", reference=reference))
        await db.flush()
        await db.execute(update(AiWallet).where(AiWallet.telegram_id == target.telegram_id).values(credits=AiWallet.credits + count))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if not reference or not await db.scalar(select(AiCreditGrant.id).where(AiCreditGrant.reference == reference)):
            raise
