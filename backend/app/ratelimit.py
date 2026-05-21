"""Rate limit + проверка квоты Free/Premium + админ-bypass.

Админы (settings.admin_usernames_set) обходят все лимиты — безлимит решений.
"""
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.core.db import get_session
from app.core.redis import get_redis
from app.models import User


# === Rate limit (через Redis) ===

RATE_LIMIT_REQUESTS = 20            # 20 запросов
RATE_LIMIT_WINDOW_SEC = 60          # за 60 секунд


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


# === Админ-чек ===

def is_admin(username: Optional[str]) -> bool:
    """Админ → безлимит. Чек по username (case-insensitive, без @)."""
    if not username:
        return False
    return username.lower().lstrip("@") in settings.admin_usernames_set


# === Квота (БД) ===

class QuotaResult:
    """Результат проверки квоты."""
    def __init__(
        self,
        allowed: bool,
        reason: str = "",
        free_remaining: int = 0,
        is_premium: bool = False,
        is_admin: bool = False,
        premium_until: Optional[datetime] = None,
    ):
        self.allowed = allowed
        self.reason = reason
        self.free_remaining = free_remaining
        self.is_premium = is_premium
        self.is_admin = is_admin
        self.premium_until = premium_until


async def get_or_create_user(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    language_code: Optional[str] = None,
) -> User:
    """Получить юзера или создать. Обновляет username/имя если изменились."""
    async with get_session() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        user = (await session.execute(stmt)).scalar_one_or_none()
        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                language_code=language_code,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            logger.info(f"New user: {telegram_id} @{username}")
        else:
            # обновляем username/имя если поменялись
            changed = False
            if username and user.username != username:
                user.username = username
                changed = True
            if first_name and user.first_name != first_name:
                user.first_name = first_name
                changed = True
            if last_name and user.last_name != last_name:
                user.last_name = last_name
                changed = True
            if changed:
                await session.commit()
        return user


async def check_quota(telegram_id: int, username: Optional[str] = None) -> QuotaResult:
    """Проверить может ли юзер сделать запрос."""
    now = datetime.now(timezone.utc)

    # Админ → безлимит мгновенно
    if is_admin(username):
        return QuotaResult(allowed=True, is_admin=True)

    async with get_session() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        user = (await session.execute(stmt)).scalar_one_or_none()
        if user is None:
            # Новый юзер — пускаем (регистрация в /start)
            return QuotaResult(
                allowed=True,
                free_remaining=settings.free_lifetime_tasks,
                is_premium=False,
            )

        # Premium активен?
        if user.has_premium(now):
            return QuotaResult(
                allowed=True,
                is_premium=True,
                premium_until=user.premium_until,
            )

        # Free tier
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


async def consume_quota(telegram_id: int, username: Optional[str] = None) -> None:
    """Инкрементировать total_solved и free_used (если не premium и не админ)."""
    if is_admin(username):
        # админ — статистику не ведём
        return

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        user = (await session.execute(stmt)).scalar_one_or_none()
        if user is None:
            return
        user.total_solved += 1
        if not user.has_premium(now):
            user.free_used += 1
        await session.commit()


async def activate_premium(telegram_id: int, duration_days: int = 30) -> datetime:
    """Активировать/продлить Premium юзера. Возвращает новую дату окончания."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    async with get_session() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        user = (await session.execute(stmt)).scalar_one_or_none()
        if user is None:
            # Юзера ещё нет — создаём заглушку. Должен быть редкий случай.
            user = User(telegram_id=telegram_id)
            session.add(user)

        # Если уже есть активная подписка — продлеваем от premium_until.
        # Если нет — от текущего момента.
        base = user.premium_until if user.premium_until and user.premium_until > now else now
        user.premium_until = base + timedelta(days=duration_days)
        await session.commit()
        await session.refresh(user)
        logger.info(f"Premium activated: {telegram_id} until {user.premium_until}")
        return user.premium_until
