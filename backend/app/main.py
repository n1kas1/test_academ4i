"""Entry point: FastAPI + aiogram webhook.

Запуск:
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Webhook от Telegram приходит на POST /webhook,
секрет проверяется через X-Telegram-Bot-Api-Secret-Token.
"""
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from loguru import logger

from app.config import settings
from app.bot import handlers
from app.core.db import close_db, init_db
from app.core.redis import close_redis, init_redis

# === Aiogram setup ===
bot = Bot(
    token=settings.telegram_bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
)
dp = Dispatcher()
dp.include_router(handlers.router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown — init DB, Redis, register webhook."""
    logger.info("Starting academ4i...")
    await init_db()
    await init_redis()

    # Зарегистрировать webhook в TG
    await bot.set_webhook(
        url=settings.webhook_url,
        secret_token=settings.telegram_webhook_secret,
        drop_pending_updates=True,
    )
    logger.info(f"Webhook set: {settings.webhook_url}")

    yield

    logger.info("Shutting down...")
    await bot.delete_webhook()
    await bot.session.close()
    await close_db()
    await close_redis()


app = FastAPI(title="academ4i", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(None),
):
    """Принимаем апдейты от Telegram."""
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(403, "Invalid secret")

    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}
