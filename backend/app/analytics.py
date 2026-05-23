"""Лёгкий лог продуктовых событий. Fire-and-forget — не влияет на UX и hot-path."""
import asyncio
import json

from loguru import logger
from sqlalchemy import text

from app.core.db import get_session

# Держим ссылки на фоновые задачи, чтобы их не собрал GC до завершения.
_background: set[asyncio.Task] = set()


async def _insert_event(telegram_id: int, event_type: str, props: dict | None) -> None:
    try:
        props_json = json.dumps(props) if props else None
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO events (id, telegram_id, type, props) "
                    "VALUES (gen_random_uuid(), :tg, :type, CAST(:props AS jsonb))"
                ),
                {"tg": telegram_id, "type": event_type, "props": props_json},
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"log_event failed ({event_type}, {telegram_id}): {e}")


def log_event(telegram_id: int, event_type: str, **props) -> None:
    """Залогировать событие в фоне. Никогда не бросает в вызывающий поток."""
    task = asyncio.create_task(_insert_event(telegram_id, event_type, props or None))
    _background.add(task)
    task.add_done_callback(_background.discard)
