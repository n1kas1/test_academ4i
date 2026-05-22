"""Telegram bot handlers.

Routing:
  /start, /menu — главное меню (reply-keyboard под клавиатурой)
  фото → check_quota → pipeline → PNG + LaTeX button → consume_quota
  Кнопки меню (BTN_*) — переход на покупку или /balance / /help
"""
import asyncio
import io
import json
import secrets
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    Message,
)
from loguru import logger

from app.ai.pipeline import solve_task_from_photo
from app.bot.keyboards import (
    BTN_BALANCE,
    BTN_BUY_PACK,
    BTN_BUY_PREMIUM,
    BTN_HELP,
    main_menu_keyboard,
    solution_keyboard,
    task_choice_keyboard,
)
from app.bot.messages import (
    MSG_ADMIN_WELCOME,
    MSG_BUY_PACK_PROMPT,
    MSG_BUY_PREMIUM_PROMPT,
    MSG_DEMO_CAPTION,
    MSG_ERROR,
    MSG_HELP,
    MSG_PROCESSING,
    MSG_QUOTA_EXCEEDED,
    MSG_START,
    msg_balance,
    msg_choose_task,
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
TASKPICK_TTL_SECONDS = 3600
SOLUTION_TTL_SECONDS = 3600

# Документы, которые принимаем как задачу: картинки и PDF.
_DOC_PDF = "application/pdf"
_MAX_DOC_BYTES = 20 * 1024 * 1024  # лимит загрузки файла Telegram-ботом

# Демо-картинка решения для /start (статический ассет).
_DEMO_PATH = Path(__file__).parent / "assets" / "demo_solution.png"


# ─────────────────────── команды ───────────────────────

async def _menu_kb(user_id: int, username: str | None):
    """Меню с учётом текущего статуса юзера (Premium/admin → без кнопок покупки)."""
    quota = await check_quota(user_id, username=username)
    return main_menu_keyboard(is_premium=quota.is_premium, is_admin=quota.is_admin), quota


@router.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    logger.info(f"/start from {user.id} @{user.username}")
    await get_or_create_user(
        telegram_id=user.id, username=user.username,
        first_name=user.first_name, last_name=user.last_name,
        language_code=user.language_code,
    )
    kb, _ = await _menu_kb(user.id, user.username)
    prefix = MSG_ADMIN_WELCOME if is_admin(user.username) else ""
    await message.answer(prefix + MSG_START, reply_markup=kb)

    # Демо-пример решения — чтобы юзер сразу понял что делать (кинуть фото).
    if _DEMO_PATH.exists():
        try:
            await message.answer_photo(FSInputFile(_DEMO_PATH), caption=MSG_DEMO_CAPTION)
        except Exception as e:
            logger.warning(f"demo photo send failed (non-fatal): {e}")
    else:
        logger.warning(f"demo image not found at {_DEMO_PATH}")


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    user = message.from_user
    kb, _ = await _menu_kb(user.id, user.username)
    await message.answer("Главное меню 👇", reply_markup=kb)


@router.message(Command("help"))
async def cmd_help(message: Message):
    user = message.from_user
    kb, _ = await _menu_kb(user.id, user.username)
    await message.answer(MSG_HELP, reply_markup=kb)


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
    quota = await check_quota(user.id, username=user.username)
    if quota.is_premium:
        until_str = quota.premium_until.strftime("%d.%m.%Y") if quota.premium_until else ""
        kb = main_menu_keyboard(is_premium=True)
        await message.answer(
            f"💎 У тебя активен Premium до <b>{until_str}</b> — безлимит решений.\n"
            f"Дополнительные пакеты не нужны.",
            reply_markup=kb,
        )
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
        kb = main_menu_keyboard(is_premium=True)
        await message.answer(
            f"💎 Premium уже активен до <b>{until_str}</b>.\n"
            f"Продлить можно после окончания срока.",
            reply_markup=kb,
        )
        return
    await message.answer(MSG_BUY_PREMIUM_PROMPT)
    await send_premium_invoice(bot, chat_id=message.chat.id)


async def _send_balance(message: Message):
    user = message.from_user
    await get_or_create_user(
        telegram_id=user.id, username=user.username,
        first_name=user.first_name, last_name=user.last_name,
    )
    quota = await check_quota(user.id, username=user.username)
    kb = main_menu_keyboard(is_premium=quota.is_premium, is_admin=quota.is_admin)
    await message.answer(msg_balance(quota), reply_markup=kb)


# ─────────────────────── фото → решение ───────────────────────

def _pdf_first_page_png(pdf_bytes: bytes) -> bytes:
    """Первая страница PDF → PNG (через poppler/pdf2image). Синхронно."""
    from pdf2image import convert_from_bytes
    images = convert_from_bytes(pdf_bytes, dpi=200, first_page=1, last_page=1)
    if not images:
        raise ValueError("PDF has no pages")
    buf = io.BytesIO()
    images[0].save(buf, format="PNG")
    return buf.getvalue()


async def _download_image(bot: Bot, file_id: str, is_pdf: bool = False) -> bytes:
    """Скачать вложение по file_id. PDF конвертируем в PNG первой страницы."""
    file = await bot.get_file(file_id)
    bio = await bot.download_file(file.file_path)
    raw = bio.read() if hasattr(bio, "read") else bio
    if is_pdf:
        return await asyncio.to_thread(_pdf_first_page_png, raw)
    return raw


def _make_status_cb(processing_msg: Message):
    """Колбэк прогресс-статуса: редактирует одно и то же сообщение по стадиям."""
    async def cb(text: str):
        try:
            await processing_msg.edit_text(text)
        except Exception:
            # "message is not modified" и прочие — не критичны
            pass
    return cb


async def _send_solution_result(
    bot: Bot, chat_id: int, processing_msg: Message, result: dict,
    *, caption: str, image_ref: dict | None = None, allow_resolve: bool = True,
) -> None:
    """Доставить решение: превью-фото первой страницы + полный PDF.

    image_ref={"file_id","is_pdf","hint"} — нужен для кнопки «перерешать».
    Квоту здесь НЕ списываем — это делает вызывающий (re-solve не списывает).
    """
    latex_text = result.get("latex", "")
    pdf_bytes = result.get("pdf")
    png_bytes = result.get("png")

    try:
        await processing_msg.delete()
    except Exception:
        pass

    # Рендер упал целиком → отдаём LaTeX текстом (хоть что-то).
    if not pdf_bytes and not png_bytes:
        logger.warning("Render failed — sending LaTeX as text")
        for ch in _split_for_telegram(latex_text or "Не удалось оформить решение."):
            await bot.send_message(chat_id, f"<pre>{_escape_html(ch)}</pre>")
        return

    # Данные решения в Redis: latex для кнопки + (опц.) ссылка на фото для «перерешать».
    token = secrets.token_urlsafe(8)
    payload = {"latex": latex_text}
    can_resolve = bool(allow_resolve and image_ref and image_ref.get("file_id"))
    if can_resolve:
        payload.update(image_ref)
    await get_redis().set(f"sol:{token}", json.dumps(payload), ex=SOLUTION_TTL_SECONDS)
    kb = solution_keyboard(token, allow_resolve=can_resolve)

    if png_bytes and pdf_bytes:
        # Превью первой страницы (быстрый взгляд) + полный PDF (чётко, целиком).
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(png_bytes, filename="preview.png"),
            caption=caption,
        )
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(pdf_bytes, filename="solution.pdf"),
            caption="📄 Полное решение — открой, чтобы увеличить и пролистать.",
            reply_markup=kb,
        )
    elif pdf_bytes:
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(pdf_bytes, filename="solution.pdf"),
            caption=caption,
            reply_markup=kb,
        )
    else:  # только превью-картинка
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(png_bytes, filename="solution.png"),
            caption=caption,
            reply_markup=kb,
        )


async def _solve_incoming(
    message: Message, bot: Bot, file_id: str, is_pdf: bool, caption: str,
) -> None:
    """Единый поток: фото или документ → решение. Лимиты, квота, статус, доставка."""
    user = message.from_user
    user_id = user.id
    username = user.username

    await get_or_create_user(
        telegram_id=user_id, username=username,
        first_name=user.first_name, last_name=user.last_name,
    )

    if not await check_rate_limit(user_id):
        await message.answer("⏱ Слишком быстро! Подожди минутку и попробуй снова.")
        return

    quota = await check_quota(user_id, username=username)
    if not quota.allowed:
        kb = main_menu_keyboard(is_premium=quota.is_premium, is_admin=quota.is_admin)
        await message.answer(MSG_QUOTA_EXCEEDED, reply_markup=kb)
        return

    try:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    except Exception:
        pass

    try:
        image_bytes = await _download_image(bot, file_id, is_pdf)
    except Exception as e:
        logger.warning(f"download/convert failed for {user_id}: {e}")
        await message.answer("😔 Не смог открыть файл. Пришли фото или PDF задачи ещё раз.")
        return

    processing_msg = await message.answer(MSG_PROCESSING)

    try:
        result = await solve_task_from_photo(
            image_bytes, user_id=user_id, user_hint=caption,
            on_status=_make_status_cb(processing_msg),
        )

        # Несколько задач и подписи нет → спрашиваем какую решать (квоту НЕ списываем).
        if result.get("needs_choice"):
            task_ids = result["task_ids"]
            token = secrets.token_urlsafe(8)
            await get_redis().set(
                f"taskpick:{token}",
                json.dumps({"file_id": file_id, "is_pdf": is_pdf, "task_ids": task_ids}),
                ex=TASKPICK_TTL_SECONDS,
            )
            try:
                await processing_msg.delete()
            except Exception:
                pass
            await message.answer(
                msg_choose_task(task_ids),
                reply_markup=task_choice_keyboard(token, task_ids),
            )
            return

        await _send_solution_result(
            bot, message.chat.id, processing_msg, result,
            caption=_build_solution_caption(quota),
            image_ref={"file_id": file_id, "is_pdf": is_pdf, "hint": caption},
            allow_resolve=True,
        )
        await consume_quota(user_id, username=username)

    except Exception as e:
        logger.exception(f"Pipeline error for user {user_id}: {e}")
        try:
            await processing_msg.edit_text(MSG_ERROR)
        except Exception:
            await message.answer(MSG_ERROR)


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    logger.info(f"Photo from {message.from_user.id} @{message.from_user.username}")
    await _solve_incoming(
        message, bot, message.photo[-1].file_id, False, (message.caption or "").strip(),
    )


@router.message(F.document)
async def handle_document(message: Message, bot: Bot):
    """Фото-как-файл (скриншот без сжатия) или PDF-страница методички."""
    doc = message.document
    mime = (doc.mime_type or "").lower()
    is_pdf = mime == _DOC_PDF
    if not (mime.startswith("image/") or is_pdf):
        await message.answer("📸 Пришли фото задачи, скриншот или PDF — другие файлы я не решаю.")
        return
    if doc.file_size and doc.file_size > _MAX_DOC_BYTES:
        await message.answer("⚠️ Файл слишком большой (макс 20 МБ). Пришли фото или PDF поменьше.")
        return
    logger.info(f"Document from {message.from_user.id} mime={mime}")
    await _solve_incoming(message, bot, doc.file_id, is_pdf, (message.caption or "").strip())


@router.callback_query(F.data.startswith("pick:"))
async def handle_pick_task(callback: CallbackQuery, bot: Bot):
    """Юзер выбрал, какую из нескольких задач на фото решить."""
    user = callback.from_user
    user_id = user.id
    username = user.username

    parts = callback.data.split(":")  # ["pick", token, idx]
    if len(parts) != 3:
        await callback.answer()
        return
    token, idx_str = parts[1], parts[2]

    raw = await get_redis().get(f"taskpick:{token}")
    if not raw:
        await callback.answer(
            "Выбор устарел (хранится 1 час). Пришли фото снова.", show_alert=True,
        )
        return
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    data = json.loads(raw)
    task_ids = data.get("task_ids", [])
    file_id = data.get("file_id")
    is_pdf = data.get("is_pdf", False)

    try:
        idx = int(idx_str)
    except ValueError:
        idx = 0
    if not (0 <= idx < len(task_ids)) or not file_id:
        await callback.answer("Что-то пошло не так, пришли фото снова.", show_alert=True)
        return
    chosen = task_ids[idx]

    # Лимиты/квота проверяем на момент выбора.
    if not await check_rate_limit(user_id):
        await callback.answer("⏱ Слишком быстро, подожди минутку.", show_alert=True)
        return
    quota = await check_quota(user_id, username=username)
    if not quota.allowed:
        await callback.answer()
        kb = main_menu_keyboard(is_premium=quota.is_premium, is_admin=quota.is_admin)
        await callback.message.answer(MSG_QUOTA_EXCEEDED, reply_markup=kb)
        return

    await callback.answer()  # убрать "часики" на кнопке
    await get_redis().delete(f"taskpick:{token}")  # выбор одноразовый

    # Сообщение-вопрос превращаем в статус-сообщение (убираем кнопки).
    processing_msg = callback.message
    try:
        await processing_msg.edit_text(f"✅ Решаю задачу <b>№{chosen}</b>…")
    except Exception:
        processing_msg = await callback.message.answer(MSG_PROCESSING)

    hint = f"реши задачу №{chosen}"
    try:
        image_bytes = await _download_image(bot, file_id, is_pdf)
        result = await solve_task_from_photo(
            image_bytes, user_id=user_id, user_hint=hint,
            on_status=_make_status_cb(processing_msg),
        )
        await _send_solution_result(
            bot, callback.message.chat.id, processing_msg, result,
            caption=_build_solution_caption(quota),
            image_ref={"file_id": file_id, "is_pdf": is_pdf, "hint": hint},
            allow_resolve=True,
        )
        await consume_quota(user_id, username=username)
    except Exception as e:
        logger.exception(f"Pick-task pipeline error for user {user_id}: {e}")
        try:
            await processing_msg.edit_text(MSG_ERROR)
        except Exception:
            await callback.message.answer(MSG_ERROR)


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


async def _load_sol(token: str) -> dict | None:
    """Прочитать данные решения из Redis (sol:{token})."""
    raw = await get_redis().get(f"sol:{token}")
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:
        return None


@router.callback_query(F.data.startswith("latex:"))
async def handle_show_latex(callback: CallbackQuery):
    token = callback.data.removeprefix("latex:")
    data = await _load_sol(token)
    latex_text = (data or {}).get("latex")

    if not latex_text:
        await callback.answer(
            "LaTeX недоступен (хранится 1 час). Кинь задачу снова.",
            show_alert=True,
        )
        return

    for ch in _split_for_telegram(latex_text, max_len=3500):
        await callback.message.answer(f"<pre>{_escape_html(ch)}</pre>", parse_mode="HTML")
    await callback.answer("LaTeX отправлен — можно копировать")


@router.callback_query(F.data.startswith("resolve:"))
async def handle_resolve(callback: CallbackQuery, bot: Bot):
    """«Перерешать» — одна бесплатная попытка на решение, мимо кэша, с thinking."""
    user = callback.from_user
    user_id = user.id

    token = callback.data.removeprefix("resolve:")
    data = await _load_sol(token)
    if not data or not data.get("file_id"):
        await callback.answer(
            "Это решение уже перерешано или устарело. Пришли задачу снова.",
            show_alert=True,
        )
        return

    if not await check_rate_limit(user_id):
        await callback.answer("⏱ Слишком быстро, подожди минутку.", show_alert=True)
        return

    await callback.answer("🔄 Перерешиваю заново…")
    await get_redis().delete(f"sol:{token}")  # одноразово — одна бесплатная попытка
    try:
        await callback.message.edit_reply_markup(reply_markup=None)  # снять кнопки со старого
    except Exception:
        pass

    processing_msg = await callback.message.answer("🔄 Решаю заново, перепроверяю вычисления…")
    hint = (data.get("hint") or "").strip()
    resolve_hint = (hint + " Перепроверь вычисления, реши заново внимательно.").strip()
    try:
        image_bytes = await _download_image(bot, data["file_id"], data.get("is_pdf", False))
        result = await solve_task_from_photo(
            image_bytes, user_id=user_id, user_hint=resolve_hint,
            on_status=_make_status_cb(processing_msg),
            skip_cache=True, force_thinking=True,
        )
        # Re-solve бесплатный → квоту НЕ списываем, и повторную кнопку не вешаем.
        await _send_solution_result(
            bot, callback.message.chat.id, processing_msg, result,
            caption="🔄 Готово — перерешано заново (квота не списана).",
            allow_resolve=False,
        )
    except Exception as e:
        logger.exception(f"Resolve error for user {user_id}: {e}")
        try:
            await processing_msg.edit_text(MSG_ERROR)
        except Exception:
            await callback.message.answer(MSG_ERROR)


@router.message(F.text)
async def handle_text(message: Message):
    """Любой текст не из меню — подсказка."""
    user = message.from_user
    kb, _ = await _menu_kb(user.id, user.username)
    await message.answer(
        "📸 Кинь <b>фото</b> задачи — решу пошагово.\n"
        "Или выбери из меню под клавиатурой 👇",
        reply_markup=kb,
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
