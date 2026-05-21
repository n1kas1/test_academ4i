"""Rate limit + проверка квоты Free/Premium.

Rate limit (анти-спам): не больше 10 запросов в минуту от юзера через Redis.
Квота: проверка premium_until из БД, инкремент free_used.
"""
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.core.db import get_session
from app.core.redis import get_redis
from app.models import User


# === Rate limit (через Redis) ===

RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW_SEC = 60


async def check_rate_limit(telegram_id: int) -> bool:
    """True если можно. False если превысил."""
    redis = get_redis()
    key = f"rl:user:{telegram_id}"

    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, RATE_LIMIT_WINDOW_SEC)

    if count > RATE_LIMIT_REQUESTS:
        logger.warning(f"Rate limit hit: user={telegram_id} count={count}")
        return False
    return True


# === Квота (БД) ===

class QuotaResult:
    """Результат проверки квоты."""
    def __init__(self, allowed: bool, reason: str = "", free_remaining: int = 0, is_premium: bool = False):
        self.allowed = allowed
        self.reason = reason
        self.free_remaining = free_remaining
        self.is_premium = is_premium


async def get_or_create_user(telegram_id: int, **profile) -> User:
    """Получить юзера по telegram_id, или создать если новый."""
    async with get_session() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        user = (await session.execute(stmt)).scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id, **profile)
            session.add(user)
            await session.commit()
            await session.refresh(user)
            logger.info(f"New user created: {telegram_id}")
        return user


async def check_quota(telegram_id: int) -> QuotaResult:
    """Проверить может ли юзер сделать запрос."""
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        user = (await session.execute(stmt)).scalar_one_or_none()
        if user is None:
            # Новый юзер — пускаем, регистрация в /start handler
            return QuotaResult(
                allowed=True,
                free_remaining=settings.free_lifetime_tasks,
                is_premium=False,
            )

        # Premium активен?
        if user.has_premium(now):
            return QuotaResult(allowed=True, is_premium=True)

        # Free tier — 5 задач lifetime
        remaining = user.free_remaining(settings.free_lifetime_tasks)
        if remaining > 0:
            return QuotaResult(
                allowed=True,
                free_remaining=remaining,
                is_premium=False,
            )

        return QuotaResult(
            allowed=False,
            reason="free_exhausted",
            free_remaining=0,
            is_premium=False,
        )


async def consume_quota(telegram_id: int) -> None:
    """Инкрементировать free_used и total_solved после успешного решения."""
    now = datetime.now(timezone.utc)
    async with get_session() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        user = (await session.execute(stmt)).scalar_one()

        user.total_solved += 1
        if not user.has_premium(now):
            user.free_used += 1

        await session.commit()
