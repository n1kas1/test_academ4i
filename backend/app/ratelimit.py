"""Rate limit + проверка квоты Free/Premium + админ-bypass.

Админы (settings.admin_usernames_set) обходят все лимиты — безлимит решений.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import select, text

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
        credits: int = 0,
        is_premium: bool = False,
        is_admin: bool = False,
        premium_until: Optional[datetime] = None,
        free_resets_at: Optional[datetime] = None,
    ):
        self.allowed = allowed
        self.reason = reason
        self.free_remaining = free_remaining
        self.credits = credits
        self.is_premium = is_premium
        self.is_admin = is_admin
        self.premium_until = premium_until
        self.free_resets_at = free_resets_at

    @property
    def total_remaining(self) -> int:
        """Сколько решений всего доступно (credits + free)."""
        return self.credits + self.free_remaining


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
                free_remaining=settings.free_tasks_per_week,
                is_premium=False,
            )

        limit = settings.free_tasks_per_week
        window = settings.free_window_days

        # Premium активен?
        if user.has_premium(now):
            return QuotaResult(
                allowed=True,
                is_premium=True,
                premium_until=user.premium_until,
                credits=user.credits,
            )

        # Купленные credits идут первыми
        if user.credits > 0:
            return QuotaResult(
                allowed=True,
                credits=user.credits,
                free_remaining=user.free_remaining(now, limit, window),
            )

        # Free tier (скользящее окно)
        remaining = user.free_remaining(now, limit, window)
        if remaining > 0:
            return QuotaResult(
                allowed=True,
                free_remaining=remaining,
                credits=0,
                is_premium=False,
                free_resets_at=user.free_resets_at(window),
            )

        return QuotaResult(
            allowed=False,
            reason="free_exhausted",
            free_remaining=0,
            credits=0,
            is_premium=False,
            free_resets_at=user.free_resets_at(window),
        )


async def consume_quota(telegram_id: int, username: Optional[str] = None) -> None:
    """Инкрементировать total_solved и списать одну единицу квоты.
    Порядок списания: premium (ничего) → credits → free (скользящее окно).

    Списание атомарно — одним UPDATE с CASE-выражениями. Все ветки CASE
    вычисляются против ТЕКУЩИХ значений строки (Postgres так считает SET),
    плюс строка блокируется на время апдейта — поэтому параллельные запросы
    не теряют декремент и не уводят credits в минус.

    Free: если окно истекло (или ещё не открыто) — открываем новое
    (free_window_start = NOW(), free_used = 1); иначе free_used += 1. Потолок
    не превышается, т.к. consume вызывается только после успешного check_quota.
    """
    if is_admin(username):
        return

    # NULL premium_until: `NULL > NOW()` → NULL → ветка не матчится → идём дальше
    # (т.е. отсутствие подписки трактуется как "не premium"). Это и нужно.
    sql = text("""
        UPDATE users SET
            total_solved = total_solved + 1,
            credits = CASE
                WHEN premium_until > NOW() THEN credits
                WHEN credits > 0 THEN credits - 1
                ELSE credits END,
            free_window_start = CASE
                WHEN premium_until > NOW() THEN free_window_start
                WHEN credits > 0 THEN free_window_start
                WHEN free_window_start IS NULL
                     OR free_window_start <= NOW() - make_interval(days => :wd) THEN NOW()
                ELSE free_window_start END,
            free_used = CASE
                WHEN premium_until > NOW() THEN free_used
                WHEN credits > 0 THEN free_used
                WHEN free_window_start IS NULL
                     OR free_window_start <= NOW() - make_interval(days => :wd) THEN 1
                ELSE free_used + 1 END
        WHERE telegram_id = :tg
    """)
    async with get_session() as session:
        await session.execute(sql, {"tg": telegram_id, "wd": settings.free_window_days})
        await session.commit()


async def _apply_credits(session, telegram_id: int, amount: int) -> int:
    """Начислить credits в рамках переданной сессии. Коммитит вызывающий."""
    stmt = select(User).where(User.telegram_id == telegram_id)
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id, credits=amount)
        session.add(user)
        await session.flush()
        return amount
    user.credits += amount
    await session.flush()
    return user.credits


async def add_credits(telegram_id: int, amount: int, session=None) -> int:
    """Начислить N задач юзеру (после оплаты пакета). Возвращает новое значение credits.

    Если передана session — работает в ней без коммита (коммитит вызывающий,
    чтобы начисление было атомарно с записью платежа). Иначе — своя транзакция.
    """
    if session is not None:
        return await _apply_credits(session, telegram_id, amount)
    async with get_session() as s:
        result = await _apply_credits(s, telegram_id, amount)
        await s.commit()
        logger.info(f"Credits +{amount} for {telegram_id}, total={result}")
        return result


async def _apply_premium(session, telegram_id: int, duration_days: int) -> datetime:
    """Активировать/продлить Premium в рамках переданной сессии. Коммитит вызывающий."""
    now = datetime.now(timezone.utc)
    stmt = select(User).where(User.telegram_id == telegram_id)
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None:
        # Юзера ещё нет — создаём заглушку. Должен быть редкий случай.
        user = User(telegram_id=telegram_id)
        session.add(user)
    # Активная подписка → продлеваем от premium_until; иначе → от текущего момента.
    base = user.premium_until if user.premium_until and user.premium_until > now else now
    user.premium_until = base + timedelta(days=duration_days)
    await session.flush()
    return user.premium_until


async def activate_premium(telegram_id: int, duration_days: int = 30, session=None) -> datetime:
    """Активировать/продлить Premium юзера. Возвращает новую дату окончания.

    Если передана session — работает в ней без коммита (см. add_credits).
    """
    if session is not None:
        return await _apply_premium(session, telegram_id, duration_days)
    async with get_session() as s:
        result = await _apply_premium(s, telegram_id, duration_days)
        await s.commit()
        logger.info(f"Premium activated: {telegram_id} until {result}")
        return result
