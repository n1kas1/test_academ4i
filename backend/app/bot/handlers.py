"""Telegram bot handlers.

Сейчас — каркас с заглушками. Реальная логика будет добавлена
по мере готовности AI-pipeline (app/ai/pipeline.py).
"""
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from app.ai.pipeline import solve_task_from_photo
from app.bot.messages import (
    MSG_START,
    MSG_HELP,
    MSG_PROCESSING,
    MSG_QUOTA_EXCEEDED,
    MSG_ERROR,
)

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message):
    """Первый контакт. Регистрируем юзера, запускаем 7-дневный триал."""
    # TODO: записать в БД user_id, started_trial_at = now()
    logger.info(f"/start from {message.from_user.id}")
    await message.answer(MSG_START)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(MSG_HELP)


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    """Оформление подписки через TG Stars."""
    # TODO: app/payments/tg_stars.py — отправка invoice
    await message.answer("⏳ Подписки скоро будут\\. Сейчас триал на 7 дней")


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    """Главный flow — фото задачи → решение."""
    user_id = message.from_user.id
    logger.info(f"Photo from {user_id}")

    # TODO: проверка квоты в Redis (триал активен? подписка?)
    # quota_ok = await check_quota(user_id)
    # if not quota_ok:
    #     await message.answer(MSG_QUOTA_EXCEEDED)
    #     return

    # Скачиваем фото
    photo = message.photo[-1]  # самое большое
    file = await bot.get_file(photo.file_id)
    photo_io = await bot.download_file(file.file_path)
    photo_bytes = photo_io.read() if hasattr(photo_io, "read") else photo_io

    # Подпись к фото юзера может содержать уточнение ("это задача 3.7")
    caption = (message.caption or "").strip()

    # Уведомление "обрабатываю"
    processing_msg = await message.answer(MSG_PROCESSING)

    try:
        solution = await solve_task_from_photo(
            photo_bytes,
            user_id=user_id,
            user_hint=caption,
        )
        await processing_msg.edit_text(solution, parse_mode="MarkdownV2")
    except Exception as e:
        logger.exception(f"Pipeline error for user {user_id}: {e}")
        await processing_msg.edit_text(MSG_ERROR)


@router.message(F.text)
async def handle_text(message: Message):
    """Текстовые сообщения — пока подсказка кинуть фото."""
    await message.answer(
        "📸 Кинь *фото* задачи — решу пошагово\\.\n"
        "Команды: /help, /subscribe"
    )
