"""Aiogram outer-middleware: привязывает user_id + username к loguru-контексту.

Внутри блока `logger.contextualize(...)` любая запись лога (вглубь pipeline:
claude, deepseek, retrieval, render — да хоть db) получит в `record.extra` поля
`tg` и `user`. Формат loguru настроен показывать их в каждой строке.

Используется outer_middleware на dp.update, чтобы установить контекст ДО
любого роутера (включая admin / tg_stars). Контекст пробрасывается через
asyncio.create_task — PEP 567 копирует contextvars в дочерний task.
"""
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from loguru import logger


# Поля Update, в которых может лежать обёртка с .from_user. Порядок — по частоте
# в нашем боте (message > callback > остальное).
_EVENT_FIELDS = ("message", "callback_query", "edited_message", "pre_checkout_query")


def _extract_user(event: TelegramObject) -> tuple[int | str, str]:
    """(tg_id, username|—) из апдейта/события. Duck-typing — не привязываемся к
    конкретному классу aiogram (помогает в unit-тестах и при апгрейдах SDK)."""
    # Сам объект может уже иметь from_user (Message, CallbackQuery передаются
    # сюда из inner-middleware).
    user = getattr(event, "from_user", None)
    if user is None:
        for attr in _EVENT_FIELDS:
            obj = getattr(event, attr, None)
            if obj is not None:
                u = getattr(obj, "from_user", None)
                if u is not None:
                    user = u
                    break
    if user is None:
        return "—", "—"
    return user.id, (user.username or "—")


class UserContextMiddleware(BaseMiddleware):
    """outer_middleware на dp.update — оборачивает обработку каждого апдейта в
    logger.contextualize(tg=..., user=...). Контекст пробрасывается во ВСЕ
    логи pipeline, AI-клиентов, рендера и т.д. без таскания аргумента."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg, username = _extract_user(event)
        with logger.contextualize(tg=tg, user=username):
            return await handler(event, data)
