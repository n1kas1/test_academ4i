"""Asyncpg pool через SQLAlchemy."""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from loguru import logger

from app.config import settings

_engine = None
_session_maker = None


def _unique_stmt_name():
    """Уникальное имя prepared statement — обход бага pgbouncer/Supabase pooler."""
    return f"__asyncpg_{uuid.uuid4().hex}__"


async def init_db():
    global _engine, _session_maker
    # Transaction pooler от Supabase шарит одно соединение между запросами →
    # имена prepared statements конфликтуют. Решение: уникальные имена.
    # Также отключаем кэш statement'ов (на всякий случай).
    # search_path: pgvector у Supabase ставится в schema "extensions" — добавляем
    # её в search_path, чтобы тип `vector` находился без префикса.
    _engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": _unique_stmt_name,
            "server_settings": {"search_path": "public,extensions"},
        },
    )
    _session_maker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    logger.info("DB pool initialized")


async def close_db():
    global _engine
    if _engine:
        await _engine.dispose()
        logger.info("DB pool closed")


def get_session() -> AsyncSession:
    """Получить сессию (для DI или вручную)."""
    if _session_maker is None:
        raise RuntimeError("DB not initialized")
    return _session_maker()
