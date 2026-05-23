"""Фоновые уведомления о Premium: «скоро закончится» и «закончился».

Запускается как asyncio-задача из main.py (lifespan). Раз в несколько часов
смотрит в БД, кому пора слать уведомление, и шлёт — по одному разу на каждое
событие. Дедуп — через Redis (ключ привязан к значению premium_until, поэтому
после продления юзер снова получит напоминание уже про новый срок).

БД-схему НЕ меняем (никаких миграций): состояние «уже уведомлён» живёт в Redis.
"""
import asyncio
from datetime import datetime

from aiogram import Bot
from app.notify import SEND_DELAY_SEC, send_one
from loguru import logger
from sqlalchemy import text

from app.bot.keyboards import renew_premium_keyboard
from app.bot.messages import MSG_PREMIUM_EXPIRED, MSG_PREMIUM_EXPIRING
from app.core.db import get_session
from app.core.redis import get_redis

CHECK_INTERVAL_SEC = 3 * 3600        # проверяем раз в 3 часа
_DEDUP_TTL_SEC = 40 * 24 * 3600      # ключи дедупа живут ~40 дней (дольше периода подписки)
_STARTUP_DELAY_SEC = 60              # дать приложению подняться перед первой проверкой
# Скоро закончится: premium_until в ближайшие 2 дня.
_SQL_EXPIRING = (
    "SELECT telegram_id, premium_until FROM users "
    "WHERE premium_until > NOW() AND premium_until <= NOW() + interval '2 days'"
)
# Только что закончился: за последние 3 дня (окно, чтобы при первом запуске
# не завалить уведомлениями всех, у кого Premium истёк давно).
_SQL_EXPIRED = (
    "SELECT telegram_id, premium_until FROM users "
    "WHERE premium_until <= NOW() AND premium_until > NOW() - interval '3 days'"
)


# (kind, sql, текст) каждого типа уведомления — порядок прохода.
_EVENTS = (
    ("remind", _SQL_EXPIRING, MSG_PREMIUM_EXPIRING),
    ("expired", _SQL_EXPIRED, MSG_PREMIUM_EXPIRED),
)


def _key(kind: str, tg: int, until: datetime) -> str:
    return f"premind:{kind}:{tg}:{int(until.timestamp())}"


async def _notify(bot: Bot, kind: str, sql: str, text_msg: str) -> None:
    async with get_session() as session:
        rows = (await session.execute(text(sql))).all()

    redis = get_redis()
    sent = 0
    for tg, until in rows:
        key = _key(kind, tg, until)
        if await redis.exists(key):
            continue
        if await send_one(bot, tg, text_msg, reply_markup=renew_premium_keyboard()):
            await redis.set(key, "1", ex=_DEDUP_TTL_SEC)  # помечаем только при успехе
            sent += 1
            await asyncio.sleep(SEND_DELAY_SEC)
    if rows:
        logger.info(f"premium notify {kind}: candidates={len(rows)} sent={sent}")


async def run_premium_checks(bot: Bot) -> None:
    """Один проход: разослать напоминания о скором конце и об окончании."""
    for kind, sql, text_msg in _EVENTS:
        await _notify(bot, kind, sql, text_msg)


async def premium_notifier_loop(bot: Bot) -> None:
    """Бесконечный цикл (отменяется при shutdown)."""
    await asyncio.sleep(_STARTUP_DELAY_SEC)
    while True:
        try:
            await run_premium_checks(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"premium notifier cycle failed: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SEC)
