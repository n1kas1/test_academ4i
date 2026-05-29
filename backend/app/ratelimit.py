"""Rate limit + кредитная квота + админ-bypass.

Админы (settings.admin_usernames_set) обходят все лимиты — безлимит решений.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
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


# === Daily cap (free-mode защита от абьюза, UTC-сутки) ===

def _daily_cap_key(telegram_id: int) -> str:
    now = datetime.now(timezone.utc)
    return f"dailycap:{telegram_id}:{now:%Y%m%d}"


async def get_daily_used(telegram_id: int) -> int:
    """Сколько решений юзер уже использовал в UTC-сутках. Read-only."""
    raw = await get_redis().get(_daily_cap_key(telegram_id))
    return int(raw or 0)


async def check_daily_cap(telegram_id: int, cap: int, username: Optional[str] = None) -> tuple[bool, int]:
    """Проверить можно ли ещё решить сегодня (БЕЗ инкремента).

    Возвращает (ok, used). Админ → (True, 0). Инкремент — `bump_daily_used` после
    успешной доставки (как consume_credits).
    """
    if is_admin(username):
        return True, 0
    used = await get_daily_used(telegram_id)
    return (used < cap), used


async def bump_daily_used(telegram_id: int, username: Optional[str] = None) -> int:
    """Инкрементировать дневной счётчик. Вызывать после успешного solve."""
    if is_admin(username):
        return 0
    redis = get_redis()
    key = _daily_cap_key(telegram_id)
    n = await redis.incr(key)
    if n == 1:
        await redis.expire(key, 26 * 3600)  # TTL чуть больше суток
    return n


# === Админ-чек ===

def is_admin(username: Optional[str]) -> bool:
    """Админ → безлимит. Чек по username (case-insensitive, без @)."""
    if not username:
        return False
    return username.lower().lstrip("@") in settings.admin_usernames_set


# === Пользователь ===

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
            # Новому юзеру — trial-кредиты (один раз, при создании).
            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                language_code=language_code,
                credits=settings.trial_credits,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            logger.info(f"New user: {telegram_id} @{username} (trial +{settings.trial_credits} credits)")
        else:
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


# === Кредитная квота ===

@dataclass
class CreditStatus:
    """Баланс кредитов юзера для UI и проверок."""
    credits: int = 0
    is_admin: bool = False

    def can_afford(self, cost: int) -> bool:
        return self.is_admin or self.credits >= cost


async def get_credit_status(telegram_id: int, username: Optional[str] = None) -> CreditStatus:
    """Текущий баланс кредитов (админ → безлимит)."""
    if is_admin(username):
        return CreditStatus(is_admin=True)
    async with get_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )).scalar_one_or_none()
        return CreditStatus(credits=(user.credits if user else 0))


async def consume_credits(telegram_id: int, cost: int, username: Optional[str] = None) -> bool:
    """Атомарно списать `cost` кредитов и инкрементировать total_solved.

    Возвращает True если списано, False если не хватило. Условие `credits >= cost`
    в самом UPDATE + блокировка строки → параллельные запросы не уводят в минус.
    Админ → безлимит: ничего не списываем, всегда True.
    """
    if is_admin(username):
        return True
    sql = text("""
        UPDATE users
        SET credits = credits - :cost,
            total_solved = total_solved + 1
        WHERE telegram_id = :tg AND credits >= :cost
        RETURNING credits
    """)
    async with get_session() as session:
        row = (await session.execute(sql, {"tg": telegram_id, "cost": cost})).first()
        await session.commit()
    if row is not None:
        logger.info(f"Credits -{cost} for {telegram_id}, left={row[0]}")
        return True
    logger.info(f"Insufficient credits for {telegram_id} (need {cost})")
    return False


# === Начисление кредитов (после оплаты пакета) ===

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
    """Начислить N кредитов юзеру (после оплаты пакета). Возвращает новое значение credits.

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
