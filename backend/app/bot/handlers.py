"""Telegram bot handlers.

Routing:
  /start, /menu — главное меню (reply-keyboard под клавиатурой)
  фото → check_quota → pipeline → PNG + LaTeX button → consume_quota
  Кнопки меню (BTN_*) — переход на покупку или /balance / /help
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
from app.bot.keyboards import (
    BTN_BALANCE,
    BTN_BUY_PACK,
    BTN_BUY_PREMIUM,
    BTN_HELP,
    latex_view_keyboard,
    main_menu_keyboard,
)
from app.bot.messages import (
    MSG_ADMIN_WELCOME,
    MSG_BUY_PACK_PROMPT,
    MSG_BUY_PREMIUM_PROMPT,
    MSG_ERROR,
    MSG_HELP,
    MSG_PROCESSING,
    MSG_QUOTA_EXCEEDED,
    MSG_START,
    msg_balance,
)
from app.config import settings
from app.core.redis import get_redis
from app.payments.tg_stars import send_pack_invoice, send_premium_invoice
from app.ratelimit import (
    check_quota,
    check_rate_limit,
    consume_quota,
    get_or_create_user,
    is_admin,
)

router = Router()
LATEX_TTL_SECONDS = 3600


# ─────────────────────── команды ───────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    logger.info(f"/start from {user.id} @{user.username}")
    await get_or_create_user(
        telegram_id=user.id, username=user.username,
        first_name=user.first_name, last_name=user.last_name,
        language_code=user.language_code,
    )
    prefix = MSG_ADMIN_WELCOME if is_admin(user.username) else ""
    await message.answer(prefix + MSG_START, reply_markup=main_menu_keyboard())


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("Главное меню 👇", reply_markup=main_menu_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(MSG_HELP, reply_markup=main_menu_keyboard())


@router.message(Command("balance"))
async def cmd_balance(message: Message):
    await _send_balance(message)


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message, bot: Bot):
    """Алиас для покупки Premium через команду."""
    await _start_premium_purchase(message, bot)


# ─────────────────────── кнопки меню ───────────────────────

@router.message(F.text == BTN_BALANCE)
async def menu_balance(message: Message):
    await _send_balance(message)


@router.message(F.text == BTN_HELP)
async def menu_help(message: Message):
    await message.answer(MSG_HELP)


@router.message(F.text == BTN_BUY_PACK)
async def menu_buy_pack(message: Message, bot: Bot):
    user = message.from_user
    if is_admin(user.username):
        await message.answer("👑 У тебя безлимит как у админа — покупки не нужны.")
        return
    await message.answer(MSG_BUY_PACK_PROMPT)
    await send_pack_invoice(bot, chat_id=message.chat.id)


@router.message(F.text == BTN_BUY_PREMIUM)
async def menu_buy_premium(message: Message, bot: Bot):
    await _start_premium_purchase(message, bot)


async def _start_premium_purchase(message: Message, bot: Bot):
    user = message.from_user
    if is_admin(user.username):
        await message.answer("👑 У тебя уже безлимит как у админа.")
        return
    quota = await check_quota(user.id, username=user.username)
    if quota.is_premium:
        until_str = quota.premium_until.strftime("%d.%m.%Y") if quota.premium_until else ""
        await message.answer(
            f"💎 Premium уже активен до <b>{until_str}</b>.\n"
            f"Оплата продлит ещё на 30 дней."
        )
    await message.answer(MSG_BUY_PREMIUM_PROMPT)
    await send_premium_invoice(bot, chat_id=message.chat.id)


async def _send_balance(message: Message):
    user = message.from_user
    await get_or_create_user(
        telegram_id=user.id, username=user.username,
        first_name=user.first_name, last_name=user.last_name,
    )
    quota = await check_quota(user.id, username=user.username)
    await message.answer(msg_balance(quota), reply_markup=main_menu_keyboard())


# ─────────────────────── фото → решение ───────────────────────

@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    user = message.from_user
    user_id = user.id
    username = user.username
    logger.info(f"Photo from {user_id} @{username}")

    await get_or_create_user(
        telegram_id=user_id, username=username,
        first_name=user.first_name, last_name=user.last_name,
    )

    if not await check_rate_limit(user_id):
        await message.answer("⏱ Слишком быстро! Подожди минутку и попробуй снова.")
        return

    quota = await check_quota(user_id, username=username)
    if not quota.allowed:
        await message.answer(MSG_QUOTA_EXCEEDED, reply_markup=main_menu_keyboard())
        return

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    photo_io = await bot.download_file(file.file_path)
    photo_bytes = photo_io.read() if hasattr(photo_io, "read") else photo_io
    caption = (message.caption or "").strip()

    processing_msg = await message.answer(MSG_PROCESSING)

    try:
        result = await solve_task_from_photo(
            photo_bytes, user_id=user_id, user_hint=caption,
        )
        latex_text = result.get("latex", "")
        png_bytes = result.get("png")

        if not png_bytes:
            logger.warning(f"PNG render failed for user {user_id}")
            await processing_msg.delete()
            chunks = _split_for_telegram(latex_text or "Не удалось получить решение.")
            for ch in chunks:
                await message.answer(f"<pre>{_escape_html(ch)}</pre>", parse_mode="HTML")
        else:
            token = secrets.token_urlsafe(8)
            redis = get_redis()
            await redis.set(f"latex:{token}", latex_text, ex=LATEX_TTL_SECONDS)

            cap_text = _build_solution_caption(quota)
            await processing_msg.delete()
            await message.answer_photo(
                photo=BufferedInputFile(png_bytes, filename="solution.png"),
                caption=cap_text,
                reply_markup=latex_view_keyboard(token),
            )

        await consume_quota(user_id, username=username)

    except Exception as e:
        logger.exception(f"Pipeline error for user {user_id}: {e}")
        try:
            await processing_msg.edit_text(MSG_ERROR)
        except Exception:
            await message.answer(MSG_ERROR)


def _build_solution_caption(quota) -> str:
    if quota.is_admin:
        return "✅ Готово · 👑 админ"
    if quota.is_premium:
        return "✅ Готово · 💎 Premium"
    # Списываем сначала credits, потом free — покажем что останется
    if quota.credits > 0:
        remaining = quota.credits - 1
        return f"✅ Готово · купленных задач осталось: {remaining}"
    remaining_after = max(0, quota.free_remaining - 1)
    if remaining_after == 0:
        return (
            "✅ Готово · бесплатные решения закончились.\n"
            "Выбери в меню пакет 79⭐ или Premium 149⭐"
        )
    return f"✅ Готово · осталось {remaining_after}/{settings.free_lifetime_tasks} бесплатных"


@router.callback_query(F.data.startswith("latex:"))
async def handle_show_latex(callback: CallbackQuery):
    token = callback.data.removeprefix("latex:")
    redis = get_redis()
    latex_text = await redis.get(f"latex:{token}")

    if not latex_text:
        await callback.answer(
            "LaTeX недоступен (хранится 1 час). Кинь задачу снова.",
            show_alert=True,
        )
        return

    if isinstance(latex_text, bytes):
        latex_text = latex_text.decode("utf-8")

    chunks = _split_for_telegram(latex_text, max_len=3500)
    for ch in chunks:
        await callback.message.answer(
            f"<pre>{_escape_html(ch)}</pre>",
            parse_mode="HTML",
        )
    await callback.answer("LaTeX отправлен — можно копировать")


@router.message(F.text)
async def handle_text(message: Message):
    """Любой текст не из меню — подсказка."""
    await message.answer(
        "📸 Кинь <b>фото</b> задачи — решу пошагово.\n"
        "Или выбери из меню под клавиатурой 👇",
        reply_markup=main_menu_keyboard(),
    )


# ─── утилиты ──────────────────────────────────────────────────────────

def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_for_telegram(text: str, max_len: int = 3500) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks
