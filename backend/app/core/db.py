"""Asyncpg pool через SQLAlchemy."""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from loguru import logger

from app.config import settings

_engine = None
_session_maker = None


async def init_db():
    global _engine, _session_maker
    # Transaction pooler от Supabase не любит prepared statements asyncpg →
    # отключаем кэш через statement_cache_size=0.
    # search_path: pgvector у Supabase ставится в schema "extensions" — добавляем
    # её в search_path, чтобы тип `vector` находился без префикса.
    _engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
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
