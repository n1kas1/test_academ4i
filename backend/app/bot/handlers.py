"""Telegram bot handlers.

Главный flow:
  фото → pipeline → {png, latex}
  → send_photo(png) с inline-кнопкой "📋 Показать LaTeX"
  → при клике на кнопку — присылается отдельным сообщением сырой LaTeX
"""
import secrets

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Message,
)
from loguru import logger

from app.ai.pipeline import solve_task_from_photo
from app.bot.keyboards import latex_view_keyboard
from app.bot.messages import (
    MSG_START,
    MSG_HELP,
    MSG_PROCESSING,
    MSG_ERROR,
)
from app.core.redis import get_redis

router = Router()

# TTL хранения LaTeX в Redis для callback-кнопки (1 час хватает)
LATEX_TTL_SECONDS = 3600


@router.message(Command("start"))
async def cmd_start(message: Message):
    logger.info(f"/start from {message.from_user.id}")
    await message.answer(MSG_START)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(MSG_HELP)


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    await message.answer("⏳ Подписки скоро будут. Сейчас 5 решений бесплатно.")


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    """Главный flow — фото задачи → PNG-решение + кнопка LaTeX."""
    user_id = message.from_user.id
    logger.info(f"Photo from {user_id}")

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    photo_io = await bot.download_file(file.file_path)
    photo_bytes = photo_io.read() if hasattr(photo_io, "read") else photo_io
    caption = (message.caption or "").strip()

    processing_msg = await message.answer(MSG_PROCESSING)

    try:
        result = await solve_task_from_photo(
            photo_bytes,
            user_id=user_id,
            user_hint=caption,
        )
        latex_text = result.get("latex", "")
        png_bytes = result.get("png")

        if not png_bytes:
            # Рендер упал — отправим хотя бы LaTeX-текстом как fallback
            logger.warning(f"PNG render failed, sending LaTeX as text for user {user_id}")
            await processing_msg.delete()
            chunks = _split_for_telegram(latex_text or "Не удалось получить решение.")
            for ch in chunks:
                await message.answer(f"<pre>{_escape_html(ch)}</pre>", parse_mode="HTML")
            return

        # Сохраняем LaTeX в Redis под коротким токеном для callback
        token = secrets.token_urlsafe(8)
        redis = get_redis()
        await redis.set(f"latex:{token}", latex_text, ex=LATEX_TTL_SECONDS)

        # Отправляем PNG + кнопку "Показать LaTeX"
        await processing_msg.delete()
        await message.answer_photo(
            photo=BufferedInputFile(png_bytes, filename="solution.png"),
            caption="✅ Готово",
            reply_markup=latex_view_keyboard(token),
        )
    except Exception as e:
        logger.exception(f"Pipeline error for user {user_id}: {e}")
        try:
            await processing_msg.edit_text(MSG_ERROR)
        except Exception:
            await message.answer(MSG_ERROR)


@router.callback_query(F.data.startswith("latex:"))
async def handle_show_latex(callback: CallbackQuery):
    """Юзер нажал на кнопку — отправляем сырой LaTeX отдельным сообщением для копирования."""
    token = callback.data.removeprefix("latex:")
    redis = get_redis()
    latex_text = await redis.get(f"latex:{token}")

    if not latex_text:
        await callback.answer(
            "LaTeX уже не доступен (хранится 1 час). Кинь задачу снова.",
            show_alert=True,
        )
        return

    if isinstance(latex_text, bytes):
        latex_text = latex_text.decode("utf-8")

    # Разбиваем на чанки по 3500 символов (лимит Telegram 4096) + оборачиваем в <pre>
    chunks = _split_for_telegram(latex_text, max_len=3500)
    for ch in chunks:
        await callback.message.answer(
            f"<pre>{_escape_html(ch)}</pre>",
            parse_mode="HTML",
        )
    await callback.answer("LaTeX отправлен — можно копировать")


@router.message(F.text)
async def handle_text(message: Message):
    await message.answer(
        "📸 Кинь <b>фото</b> задачи — решу пошагово.\n"
        "Команды: /help, /subscribe"
    )


# ─── утилиты ──────────────────────────────────────────────────────────

def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_for_telegram(text: str, max_len: int = 3500) -> list[str]:
    """Дробит длинный текст на куски по max_len символов."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks
