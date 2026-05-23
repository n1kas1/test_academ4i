"""Общий троттлящий рассыльщик: учитывает Telegram flood-control (429)."""
import asyncio
from typing import Iterable, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from loguru import logger

SEND_DELAY_SEC = 0.05  # ~20 msg/s — держим темп ниже flood-лимита Telegram


async def send_one(
    bot: Bot,
    tg: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    """Отправить одно сообщение. True при успехе.

    Flood-control (429) — переждать retry_after и повторить один раз. Прочие
    ошибки (юзер заблокировал бота и т.п.) — пропустить, не валя всю рассылку.
    """
    for _ in range(2):
        try:
            await bot.send_message(tg, text, reply_markup=reply_markup)
            return True
        except TelegramRetryAfter as e:
            logger.warning(f"send flood: sleeping {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            logger.warning(f"send skip {tg}: {e}")
            return False
    return False


async def broadcast_send(
    bot: Bot,
    telegram_ids: Iterable[int],
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> tuple[int, int]:
    """Разослать text всем telegram_ids с пейсингом. Возвращает (sent, failed)."""
    sent = 0
    failed = 0
    for tg in telegram_ids:
        if await send_one(bot, tg, text, reply_markup):
            sent += 1
            await asyncio.sleep(SEND_DELAY_SEC)
        else:
            failed += 1
    return sent, failed
