"""Запуск fire-and-forget фоновых задач с удержанием ссылки (защита от GC).

asyncio не держит сильную ссылку на задачу — без этого незавершённую задачу
может собрать сборщик мусора. Поэтому держим её в множестве до завершения.
"""
import asyncio
from collections.abc import Coroutine
from typing import Any

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Создать фоновую задачу и удержать ссылку на неё до завершения."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
