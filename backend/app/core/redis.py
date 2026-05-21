"""Redis client для rate-limit, кэша и контекста диалога."""
from redis.asyncio import Redis
from loguru import logger

from app.config import settings

_redis: Redis | None = None


async def init_redis():
    global _redis
    _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    await _redis.ping()
    logger.info("Redis connected")


async def close_redis():
    global _redis
    if _redis:
        await _redis.aclose()
        logger.info("Redis closed")


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized")
    return _redis
