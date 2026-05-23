"""Telegram Stars — две покупки:

  PAYLOAD_PREMIUM = 30 дней безлимита за settings.premium_price_stars (149)
  PAYLOAD_PACK    = settings.pack_tasks разовых решений за settings.pack_price_stars (79)
"""
from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from loguru import logger
from sqlalchemy import text

from app.bot.keyboards import main_menu_keyboard
from app.config import settings
from app.core.db import get_session
from app.ratelimit import activate_premium, add_credits, is_admin

router = Router()

PAYLOAD_PREMIUM = "academ4i_premium_30d"
PAYLOAD_PREMIUM_WEEK = "academ4i_premium_7d"
PAYLOAD_PACK = "academ4i_pack_5"
PAYLOAD_PACK10 = "academ4i_pack_10"

# payload → (product-метка для БД, тип "premium"/"pack", количество дней/задач).
_GRANTS = {
    PAYLOAD_PREMIUM:      ("premium_30d", "premium", settings.premium_duration_days),
    PAYLOAD_PREMIUM_WEEK: ("premium_7d",  "premium", settings.premium_week_days),
    PAYLOAD_PACK:         ("pack_5",      "pack",    settings.pack_tasks),
    PAYLOAD_PACK10:       ("pack_10",     "pack",    settings.pack_large_tasks),
}


async def _send_invoice(bot: Bot, chat_id: int, title: str, desc: str, payload: str, amount: int) -> None:
    await bot.send_invoice(
        chat_id=chat_id, title=title, description=desc, payload=payload,
        currency="XTR", prices=[LabeledPrice(label=title, amount=amount)],
    )


async def send_premium_invoice(bot: Bot, chat_id: int) -> None:
    """Premium на 30 дней (безлимит в рамках дневного fair-use)."""
    await _send_invoice(
        bot, chat_id, "Academ4I — Premium 30 дней",
        f"Безлимит решений на 30 дней (до {settings.premium_daily_cap} задач/день). "
        "Матан, линал, алгебра, тервер, дискретка.",
        PAYLOAD_PREMIUM, settings.premium_price_stars,
    )


async def send_premium_week_invoice(bot: Bot, chat_id: int) -> None:
    """Premium на 7 дней — под сессию."""
    await _send_invoice(
        bot, chat_id, "Academ4I — Premium 7 дней",
        f"Безлимит решений на 7 дней (до {settings.premium_daily_cap} задач/день). "
        "Идеально на сессию.",
        PAYLOAD_PREMIUM_WEEK, settings.premium_week_price_stars,
    )


async def send_pack_invoice(bot: Bot, chat_id: int) -> None:
    """Пакет 5 задач без срока."""
    await _send_invoice(
        bot, chat_id, f"Academ4I — Пакет {settings.pack_tasks} задач",
        f"{settings.pack_tasks} решений сверх бесплатных. Без срока — используй когда нужно.",
        PAYLOAD_PACK, settings.pack_price_stars,
    )


async def send_pack10_invoice(bot: Bot, chat_id: int) -> None:
    """Пакет 10 задач без срока."""
    await _send_invoice(
        bot, chat_id, f"Academ4I — Пакет {settings.pack_large_tasks} задач",
        f"{settings.pack_large_tasks} решений сверх бесплатных. Без срока — используй когда нужно.",
        PAYLOAD_PACK10, settings.pack_large_price_stars,
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, bot: Bot):
    """Подтверждение перед списанием. OK для известных payload."""
    if query.invoice_payload in _GRANTS:
        logger.info(
            f"pre_checkout OK: user={query.from_user.id} "
            f"payload={query.invoice_payload} amount={query.total_amount}"
        )
        await bot.answer_pre_checkout_query(query.id, ok=True)
    else:
        logger.warning(f"pre_checkout REJECT: unknown payload {query.invoice_payload!r}")
        await bot.answer_pre_checkout_query(
            query.id, ok=False,
            error_message="Неизвестный платёж. Попробуй заново через меню.",
        )


@router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    """Платёж прошёл — активируем покупку. Идемпотентно по charge_id.

    Начисление и запись в payments — в одной транзакции: сначала «столбим»
    платёж (INSERT ... ON CONFLICT DO NOTHING RETURNING id), и только если
    вставка реально произошла — начисляем. Дубль вебхука от Telegram второй
    раз ничего не начислит.
    """
    sp = message.successful_payment
    user_id = message.from_user.id
    payload = sp.invoice_payload
    charge_id = sp.telegram_payment_charge_id

    logger.info(
        f"💎 paid: user={user_id} payload={payload} "
        f"amount={sp.total_amount} {sp.currency} charge_id={charge_id}"
    )

    grant = _GRANTS.get(payload)
    if grant is None:
        logger.error(f"successful_payment with unknown payload: {payload}")
        await message.answer("Платёж прошёл, но мы не смогли распознать что куплено. Напиши в @Academ4I_support.")
        return
    product, kind, qty = grant

    premium_until = None
    new_credits = None
    duplicate = False

    async with get_session() as session:
        # 1) Идемпотентный «захват» платежа по уникальному charge_id.
        claim = await session.execute(text("""
            INSERT INTO payments (
                telegram_id, telegram_payment_charge_id, amount_stars,
                product, premium_from, premium_until, status
            ) VALUES (:tg, :charge, :amt, :product, NOW(), NOW(), 'processing')
            ON CONFLICT (telegram_payment_charge_id) DO NOTHING
            RETURNING id
        """), {"tg": user_id, "charge": charge_id, "amt": sp.total_amount, "product": product})
        claimed = claim.first()

        if claimed is None:
            # Этот платёж уже обрабатывали — повторно НЕ начисляем.
            duplicate = True
        else:
            payment_id = claimed[0]
            # 2) Начисление в той же транзакции, что и запись платежа.
            if kind == "premium":
                premium_until = await activate_premium(user_id, duration_days=qty, session=session)
                await session.execute(text(
                    "UPDATE payments SET premium_until = :until, status = 'succeeded' WHERE id = :id"
                ), {"until": premium_until, "id": payment_id})
            else:  # pack
                new_credits = await add_credits(user_id, qty, session=session)
                await session.execute(text(
                    "UPDATE payments SET status = 'succeeded' WHERE id = :id"
                ), {"id": payment_id})
            await session.commit()

    if duplicate:
        logger.warning(f"Duplicate successful_payment ignored: user={user_id} charge_id={charge_id}")
        kb = main_menu_keyboard(is_admin=is_admin(message.from_user.username))
        await message.answer(
            "✅ Этот платёж уже был активирован ранее — повторно начислять не нужно.",
            reply_markup=kb,
        )
        return

    if kind == "premium":
        result_text = (
            f"✅ <b>Premium активирован!</b>\n\n"
            f"Безлимит (до {settings.premium_daily_cap} задач/день) до "
            f"<b>{premium_until.strftime('%d.%m.%Y %H:%M UTC')}</b>.\n\n"
            f"Кидай задачи 🎓"
        )
        is_premium_now = True
    else:
        result_text = (
            f"✅ <b>Пакет {qty} задач куплен!</b>\n\n"
            f"Доступно решений: <b>{new_credits}</b> (без срока истечения).\n\n"
            f"Кидай задачи 🎓"
        )
        is_premium_now = False

    # После Premium-покупки — клавиатура без кнопок покупки; после пакета — обычное меню.
    kb = main_menu_keyboard(
        is_premium=is_premium_now,
        is_admin=is_admin(message.from_user.username),
    )
    await message.answer(result_text, reply_markup=kb)


# === Выбор тарифа из inline-меню (buy:*) ===

@router.callback_query(F.data == "buy:premmonth")
async def cb_buy_premmonth(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await send_premium_invoice(bot, callback.message.chat.id)


@router.callback_query(F.data == "buy:premweek")
async def cb_buy_premweek(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await send_premium_week_invoice(bot, callback.message.chat.id)


@router.callback_query(F.data == "buy:pack5")
async def cb_buy_pack5(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await send_pack_invoice(bot, callback.message.chat.id)


@router.callback_query(F.data == "buy:pack10")
async def cb_buy_pack10(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await send_pack10_invoice(bot, callback.message.chat.id)
