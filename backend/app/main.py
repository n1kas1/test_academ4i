"""Entry point: FastAPI + aiogram webhook.

Запуск:
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Webhook от Telegram приходит на POST /webhook,
секрет проверяется через X-Telegram-Bot-Api-Secret-Token.
"""
import logging
import socket
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from loguru import logger

from app.config import settings
from app.bot import admin, handlers
from app.payments import tg_stars
from app.core.db import close_db, init_db
from app.core.redis import close_redis, init_redis

# === Aiogram setup ===
bot = Bot(
    token=settings.telegram_bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
# payments router идёт ПЕРВЫМ — чтобы pre_checkout_query и successful_payment
# ловились ДО обработчика photo/text сообщений
dp.include_router(tg_stars.router)
dp.include_router(admin.router)
dp.include_router(handlers.router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown — init DB, Redis, register webhook."""
    logger.info("Starting academ4i...")
    await init_db()
    await init_redis()

    # Зарегистрировать webhook в TG. НЕ блокируем старт если упало —
    # обычно webhook уже установлен у Telegram, переустановка не критична.
    # Передаём ip_address (резолвим домен локально): резолвер Telegram временами
    # не находит duckdns-домен ("Temporary failure in name resolution") и
    # setWebhook падает на деплое → бот переставал получать апдейты. Явный IP
    # это обходит (SNI остаётся доменным, сертификат Caddy подходит).
    try:
        try:
            webhook_ip = socket.gethostbyname(settings.webhook_domain)
        except OSError as e:
            webhook_ip = None
            logger.warning(f"webhook domain resolve failed locally: {e}")
        await bot.set_webhook(
            url=settings.webhook_url,
            secret_token=settings.telegram_webhook_secret,
            ip_address=webhook_ip,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook set: {settings.webhook_url} (ip={webhook_ip})")
    except Exception as e:
        logger.warning(
            f"set_webhook failed (non-fatal — likely DNS lag): {e}. "
            f"App continues; reinstall webhook later if needed."
        )

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
