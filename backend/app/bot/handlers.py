"""Telegram bot handlers (credit-модель).

Routing:
  /start, /menu — главное меню (reply-keyboard)
  фото/документ → выбор режима (стандарт/премиум) с показом баланса
  выбор режима → pipeline(mode) → доставка → списание кредитов
  Кнопки меню (BTN_*) — пакеты кредитов / баланс / помощь
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

from app.ai.pipeline import solve_task_from_photo, solve_task_from_text
from app.ai.haiku_gate import is_math_or_physics
from app.bot.keyboards import (
    BTN_BALANCE,
    BTN_BUY_CREDITS,
    BTN_HELP,
    main_menu_keyboard,
    mode_choice_keyboard,
    packages_keyboard,
    solution_keyboard,
    task_choice_keyboard,
)
from app.bot.messages import (
    MSG_ADMIN_HELP,
    MSG_ADMIN_WELCOME,
    MSG_BUY_CREDITS_PROMPT,
    MSG_DAILY_CAP_REACHED,
    MSG_DEMO_CAPTION,
    MSG_ERROR,
    MSG_HELP_CREDITS,
    MSG_HELP_FREE,
    MSG_NOT_MATH,
    MSG_OCR_FAILED_STANDARD,
    MSG_PROCESSING,
    MSG_START_CREDITS,
    MSG_START_FREE,
    MSG_TEXT_PROCESSING,
    msg_balance_credits,
    msg_balance_free,
    msg_choose_task,
    msg_insufficient_credits,
    msg_mode_prompt,
)
from app.config import settings
from app.core.redis import get_redis
from app.analytics import log_event
from app.ratelimit import (
    CreditStatus,
    bump_daily_used,
    check_daily_cap,
    check_rate_limit,
    consume_credits,
    get_credit_status,
    get_daily_used,
    get_or_create_user,
    is_admin,
)

router = Router()
PENDING_TTL_SECONDS = 3600
TASKPICK_TTL_SECONDS = 3600
SOLUTION_TTL_SECONDS = 3600

_DOC_PDF = "application/pdf"
_MAX_DOC_BYTES = 20 * 1024 * 1024

_DEMO_PATH = Path(__file__).parent / "assets" / "demo_solution.png"


def _mode_cost(mode: str) -> int:
    return settings.premium_cost if mode == "premium" else settings.standard_cost


def _mode_label(mode: str) -> str:
    return "💎 Премиум" if mode == "premium" else "⚡ Стандарт"


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
    log_event(user.id, "start")
    kb = main_menu_keyboard(is_admin=is_admin(user.username))
    prefix = MSG_ADMIN_WELCOME if is_admin(user.username) else ""
    start_body = MSG_START_FREE if settings.free_mode else MSG_START_CREDITS
    await message.answer(prefix + start_body, reply_markup=kb)

    if _DEMO_PATH.exists():
        try:
            await message.answer_photo(FSInputFile(_DEMO_PATH), caption=MSG_DEMO_CAPTION)
        except Exception as e:
            logger.warning(f"demo photo send failed (non-fatal): {e}")
    else:
        logger.warning(f"demo image not found at {_DEMO_PATH}")


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    kb = main_menu_keyboard(is_admin=is_admin(message.from_user.username))
    await message.answer("Главное меню 👇", reply_markup=kb)


@router.message(Command("help"))
async def cmd_help(message: Message):
    user = message.from_user
    base = MSG_HELP_FREE if settings.free_mode else MSG_HELP_CREDITS
    help_text = f"{base}\n\n{MSG_ADMIN_HELP}" if is_admin(user.username) else base
    await message.answer(help_text, reply_markup=main_menu_keyboard(is_admin=is_admin(user.username)))


@router.message(Command("balance"))
async def cmd_balance(message: Message):
    await _send_balance(message)


# ─────────────────────── кнопки меню ───────────────────────

@router.message(F.text == BTN_BALANCE)
async def menu_balance(message: Message):
    await _send_balance(message)


@router.message(F.text == BTN_HELP)
async def menu_help(message: Message):
    # Раньше тут всегда отдавался MSG_HELP_CREDITS — с пакетами/тарифами/звёздами.
    # В free-mode это категорически нельзя.
    await message.answer(MSG_HELP_FREE if settings.free_mode else MSG_HELP_CREDITS)


@router.message(F.text == BTN_BUY_CREDITS)
async def menu_buy_credits(message: Message):
    if settings.free_mode:
        await message.answer("✨ Бот сейчас полностью бесплатный — пакеты не нужны.")
        return
    if is_admin(message.from_user.username):
        await message.answer("👑 У тебя безлимит как у админа — кредиты не нужны.")
        return
    await message.answer(MSG_BUY_CREDITS_PROMPT, reply_markup=packages_keyboard())


async def _send_balance(message: Message):
    user = message.from_user
    await get_or_create_user(
        telegram_id=user.id, username=user.username,
        first_name=user.first_name, last_name=user.last_name,
    )
    if settings.free_mode:
        adm = is_admin(user.username)
        used = 0 if adm else await get_daily_used(user.id)
        await message.answer(
            msg_balance_free(used, is_admin=adm),
            reply_markup=main_menu_keyboard(is_admin=adm),
        )
        return
    status = await get_credit_status(user.id, username=user.username)
    await message.answer(
        msg_balance_credits(status),
        reply_markup=main_menu_keyboard(is_admin=status.is_admin),
    )


# ─────────────────────── фото → выбор режима → решение ───────────────────────

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
            pass
    return cb


async def _present_modes(
    message: Message, bot: Bot, file_id: str, is_pdf: bool, hint: str,
) -> None:
    """Фото/документ получены.

    Free-mode: проверяем daily cap → сразу решаем как standard (бесплатно).
    Credit-mode: сохраняем в Redis, предлагаем выбрать режим.
    """
    user = message.from_user
    await get_or_create_user(
        telegram_id=user.id, username=user.username,
        first_name=user.first_name, last_name=user.last_name,
    )

    if not await check_rate_limit(user.id):
        await message.answer("⏱ Слишком быстро! Подожди минутку и попробуй снова.")
        return

    # ── FREE MODE ── ничего не списываем, без выбора режима.
    if settings.free_mode:
        adm = is_admin(user.username)
        if not adm:
            ok, _used = await check_daily_cap(user.id, settings.free_daily_cap)
            if not ok:
                await message.answer(MSG_DAILY_CAP_REACHED)
                return
        processing_msg = await message.answer(MSG_PROCESSING)
        await _run_solve(
            bot, message.chat.id, processing_msg, user,
            file_id, is_pdf, hint, mode="standard", cost=0,
            status=CreditStatus(is_admin=adm),
        )
        return

    # ── CREDIT MODE ──
    status = await get_credit_status(user.id, username=user.username)
    if not status.is_admin and status.credits < settings.standard_cost:
        log_event(user.id, "paywall_shown")
        await message.answer(
            msg_insufficient_credits(status.credits, settings.standard_cost),
            reply_markup=packages_keyboard(),
        )
        return

    token = secrets.token_urlsafe(8)
    await get_redis().set(
        f"pend:{token}",
        json.dumps({"file_id": file_id, "is_pdf": is_pdf, "hint": hint}),
        ex=PENDING_TTL_SECONDS,
    )
    await message.answer(msg_mode_prompt(status), reply_markup=mode_choice_keyboard(token))


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    logger.info(f"Photo from {message.from_user.id} @{message.from_user.username}")
    await _present_modes(
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
    await _present_modes(message, bot, doc.file_id, is_pdf, (message.caption or "").strip())


def _caption(status, mode: str, cost: int) -> str:
    if status.is_admin:
        return "✅ Готово · 👑 админ"
    if settings.free_mode:
        return "✅ Готово · ✨ бесплатно"
    label = _mode_label(mode)
    left = max(0, status.credits - cost)
    return f"✅ Готово · {label} (−{cost}). Кредитов осталось: {left}"


async def _run_solve(
    bot: Bot, chat_id: int, processing_msg: Message, user,
    file_id: str, is_pdf: bool, hint: str, mode: str, cost: int, status,
) -> None:
    """Скачать, решить выбранным режимом, доставить, списать кредиты.

    Списание — только после успешной доставки. needs_choice/ocr_failed не списывают.
    """
    try:
        image_bytes = await _download_image(bot, file_id, is_pdf)
    except Exception as e:
        logger.warning(f"download/convert failed for {user.id}: {e}")
        try:
            await processing_msg.edit_text("😔 Не смог открыть файл. Пришли фото или PDF задачи ещё раз.")
        except Exception:
            await bot.send_message(chat_id, "😔 Не смог открыть файл. Пришли фото или PDF задачи ещё раз.")
        return

    try:
        result = await solve_task_from_photo(
            image_bytes, user_id=user.id, user_hint=hint,
            on_status=_make_status_cb(processing_msg), mode=mode,
        )

        # Несколько задач без подсказки → спрашиваем какую (режим переносим в payload).
        if result.get("needs_choice"):
            task_ids = result["task_ids"]
            token = secrets.token_urlsafe(8)
            await get_redis().set(
                f"taskpick:{token}",
                json.dumps({"file_id": file_id, "is_pdf": is_pdf, "task_ids": task_ids, "mode": mode}),
                ex=TASKPICK_TTL_SECONDS,
            )
            try:
                await processing_msg.delete()
            except Exception:
                pass
            await bot.send_message(
                chat_id, msg_choose_task(task_ids),
                reply_markup=task_choice_keyboard(token, task_ids),
            )
            return

        # Standard не смог распознать условие → кредиты не списываем.
        if result.get("ocr_failed"):
            try:
                await processing_msg.edit_text(MSG_OCR_FAILED_STANDARD)
            except Exception:
                await bot.send_message(chat_id, MSG_OCR_FAILED_STANDARD)
            return

        await _send_solution_result(
            bot, chat_id, processing_msg, result,
            caption=_caption(status, mode, cost),
            image_ref={"file_id": file_id, "is_pdf": is_pdf, "hint": hint, "mode": mode},
            allow_resolve=True,
        )
        if settings.free_mode:
            await bump_daily_used(user.id, username=user.username)
        else:
            await consume_credits(user.id, cost, username=user.username)
        log_event(user.id, "solve")

    except Exception as e:
        logger.exception(f"Pipeline error for user {user.id}: {e}")
        try:
            await processing_msg.edit_text(MSG_ERROR)
        except Exception:
            await bot.send_message(chat_id, MSG_ERROR)


@router.callback_query(F.data.startswith("mode:"))
async def handle_mode(callback: CallbackQuery, bot: Bot):
    """Юзер выбрал режим решения — проверяем баланс и решаем."""
    # Ack callback СРАЗУ: любая задержка ~>10c → TelegramBadRequest "query is too old"
    # и /webhook отдаёт 500 → TG ретраит → лавина 500. Алерты доставляем через
    # message.answer() ниже.
    try:
        await callback.answer()
    except Exception as e:
        logger.warning(f"callback.answer (mode) skipped: {e}")

    parts = callback.data.split(":")  # ["mode", token, "standard"|"premium"]
    if len(parts) != 3:
        return
    token, mode = parts[1], parts[2]

    raw = await get_redis().get(f"pend:{token}")
    if not raw:
        await callback.message.answer("Выбор устарел (хранится 1 час). Пришли фото снова.")
        return
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    data = json.loads(raw)

    user = callback.from_user
    cost = _mode_cost(mode)
    status = await get_credit_status(user.id, username=user.username)
    if not status.can_afford(cost):
        log_event(user.id, "paywall_shown")
        await callback.message.answer(
            msg_insufficient_credits(status.credits, cost),
            reply_markup=packages_keyboard(),
        )
        return

    if not await check_rate_limit(user.id):
        await callback.message.answer("⏱ Слишком быстро, подожди минутку.")
        return

    await get_redis().delete(f"pend:{token}")

    processing_msg = callback.message
    try:
        await processing_msg.edit_text(f"{_mode_label(mode)} · {MSG_PROCESSING}")
    except Exception:
        processing_msg = await callback.message.answer(MSG_PROCESSING)

    await _run_solve(
        bot, callback.message.chat.id, processing_msg, user,
        data["file_id"], data.get("is_pdf", False), data.get("hint", ""), mode, cost, status,
    )


@router.callback_query(F.data.startswith("pick:"))
async def handle_pick_task(callback: CallbackQuery, bot: Bot):
    """Юзер выбрал, какую из нескольких задач решить (режим уже выбран ранее)."""
    # Ack callback СРАЗУ — см. handle_mode выше.
    try:
        await callback.answer()
    except Exception as e:
        logger.warning(f"callback.answer (pick) skipped: {e}")

    parts = callback.data.split(":")  # ["pick", token, idx]
    if len(parts) != 3:
        return
    token, idx_str = parts[1], parts[2]

    raw = await get_redis().get(f"taskpick:{token}")
    if not raw:
        await callback.message.answer("Выбор устарел (хранится 1 час). Пришли фото снова.")
        return
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    data = json.loads(raw)
    task_ids = data.get("task_ids", [])
    file_id = data.get("file_id")
    is_pdf = data.get("is_pdf", False)
    mode = data.get("mode", "premium")

    try:
        idx = int(idx_str)
    except ValueError:
        idx = 0
    if not (0 <= idx < len(task_ids)) or not file_id:
        await callback.message.answer("Что-то пошло не так, пришли фото снова.")
        return
    chosen = task_ids[idx]

    user = callback.from_user
    cost = _mode_cost(mode)
    status = await get_credit_status(user.id, username=user.username)
    if not status.can_afford(cost):
        log_event(user.id, "paywall_shown")
        await callback.message.answer(
            msg_insufficient_credits(status.credits, cost),
            reply_markup=packages_keyboard(),
        )
        return
    if not await check_rate_limit(user.id):
        await callback.message.answer("⏱ Слишком быстро, подожди минутку.")
        return

    await get_redis().delete(f"taskpick:{token}")

    processing_msg = callback.message
    try:
        await processing_msg.edit_text(f"{_mode_label(mode)} · ✅ Решаю задачу <b>№{chosen}</b>…")
    except Exception:
        processing_msg = await callback.message.answer(MSG_PROCESSING)

    hint = f"реши задачу №{chosen}"
    await _run_solve(
        bot, callback.message.chat.id, processing_msg, user,
        file_id, is_pdf, hint, mode, cost, status,
    )


async def _send_solution_result(
    bot: Bot, chat_id: int, processing_msg: Message, result: dict,
    *, caption: str, image_ref: dict | None = None, allow_resolve: bool = True,
) -> None:
    """Доставить решение: превью-фото первой страницы + полный PDF.

    image_ref={"file_id","is_pdf","hint","mode"} — нужен для кнопки «перерешать».
    Кредиты здесь НЕ списываем — это делает вызывающий (re-solve не списывает).
    """
    latex_text = result.get("latex", "")
    pdf_bytes = result.get("pdf")
    png_bytes = result.get("png")

    try:
        await processing_msg.delete()
    except Exception:
        pass

    if not pdf_bytes and not png_bytes:
        # До этой ветки доходим только если даже verbatim-рендер сломался
        # (значит сама TeX-машина на сервере не работает). Юзеру в чат
        # бесполезно слать LaTeX-исходник — это его никак не спасёт.
        logger.error("ALL render tiers failed (verbatim too) — sending error msg")
        await bot.send_message(
            chat_id,
            "😔 Сейчас не получается оформить PDF. Попробуй ещё раз через минуту "
            "или напиши @manag31.",
        )
        return

    token = secrets.token_urlsafe(8)
    payload = {"latex": latex_text}
    can_resolve = bool(allow_resolve and image_ref and image_ref.get("file_id"))
    if can_resolve:
        payload.update(image_ref)
    await get_redis().set(f"sol:{token}", json.dumps(payload), ex=SOLUTION_TTL_SECONDS)
    kb = solution_keyboard(token, allow_resolve=can_resolve)

    if png_bytes and pdf_bytes:
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
    else:
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(png_bytes, filename="solution.png"),
            caption=caption,
            reply_markup=kb,
        )


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
    """«Перерешать» — одна бесплатная попытка на решение, мимо кэша, в том же режиме."""
    user = callback.from_user

    token = callback.data.removeprefix("resolve:")
    data = await _load_sol(token)
    if not data or not data.get("file_id"):
        await callback.answer(
            "Это решение уже перерешано или устарело. Пришли задачу снова.",
            show_alert=True,
        )
        return

    if not await check_rate_limit(user.id):
        await callback.answer("⏱ Слишком быстро, подожди минутку.", show_alert=True)
        return

    await callback.answer("🔄 Перерешиваю заново…")
    await get_redis().delete(f"sol:{token}")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    mode = data.get("mode", "premium")
    processing_msg = await callback.message.answer("🔄 Решаю заново, перепроверяю вычисления…")
    hint = (data.get("hint") or "").strip()
    resolve_hint = (hint + " Перепроверь вычисления, реши заново внимательно.").strip()
    try:
        image_bytes = await _download_image(bot, data["file_id"], data.get("is_pdf", False))
        result = await solve_task_from_photo(
            image_bytes, user_id=user.id, user_hint=resolve_hint,
            on_status=_make_status_cb(processing_msg),
            skip_cache=True, mode=mode,
        )
        if result.get("ocr_failed") or result.get("needs_choice"):
            try:
                await processing_msg.edit_text("Не удалось перерешать автоматически — пришли фото задачи снова.")
            except Exception:
                pass
            return
        # Re-solve бесплатный → кредиты НЕ списываем, повторную кнопку не вешаем.
        await _send_solution_result(
            bot, callback.message.chat.id, processing_msg, result,
            caption="🔄 Готово — перерешано заново (кредиты не списаны).",
            allow_resolve=False,
        )
    except Exception as e:
        logger.exception(f"Resolve error for user {user.id}: {e}")
        try:
            await processing_msg.edit_text(MSG_ERROR)
        except Exception:
            await callback.message.answer(MSG_ERROR)


@router.message(F.text)
async def handle_text(message: Message, bot: Bot):
    """Любой текстовый ввод (не команда, не кнопка меню).

    Free-mode: пускаем через Haiku-гейт (математика/физика?). Если да — решаем
    текстовый ввод напрямую через DeepSeek. Если нет — вежливый отказ.
    Credit-mode: подсказка прислать фото (мы не делаем text-input платным сейчас).
    """
    user = message.from_user
    text = (message.text or "").strip()

    if not settings.free_mode:
        kb = main_menu_keyboard(is_admin=is_admin(user.username))
        await message.answer(
            "📸 Кинь <b>фото</b> задачи — предложу выбрать режим и решу.\n"
            "Или выбери из меню под клавиатурой 👇",
            reply_markup=kb,
        )
        return

    # FREE MODE: пробуем как math/physics задачу.
    await get_or_create_user(
        telegram_id=user.id, username=user.username,
        first_name=user.first_name, last_name=user.last_name,
    )
    if not await check_rate_limit(user.id):
        await message.answer("⏱ Слишком быстро! Подожди минутку.")
        return

    adm = is_admin(user.username)
    if not adm:
        ok, _used = await check_daily_cap(user.id, settings.free_daily_cap)
        if not ok:
            await message.answer(MSG_DAILY_CAP_REACHED)
            return

    # Гейт темы (Haiku) — отсекаем не-математику/физику.
    if settings.topic_gate_enabled:
        is_math = await is_math_or_physics(text)
        if not is_math:
            await message.answer(MSG_NOT_MATH)
            return

    processing_msg = await message.answer(MSG_TEXT_PROCESSING)
    try:
        result = await solve_task_from_text(
            condition_text=text, user_id=user.id,
            on_status=_make_status_cb(processing_msg),
        )
        if result.get("empty_input"):
            try:
                await processing_msg.edit_text("📝 Условие слишком короткое — пришли больше деталей.")
            except Exception:
                pass
            return
        await _send_solution_result(
            bot, message.chat.id, processing_msg, result,
            caption=_caption(CreditStatus(is_admin=adm), "standard", 0),
            image_ref=None, allow_resolve=False,  # для text-input «перерешать» пока не делаем
        )
        await bump_daily_used(user.id, username=user.username)
        log_event(user.id, "solve")
    except Exception as e:
        logger.exception(f"text-solve error for user {user.id}: {e}")
        try:
            await processing_msg.edit_text(MSG_ERROR)
        except Exception:
            await message.answer(MSG_ERROR)


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
