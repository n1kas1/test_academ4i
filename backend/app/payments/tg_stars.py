"""Telegram Stars — две покупки:

  PAYLOAD_PREMIUM = 30 дней безлимита за settings.premium_price_stars (149)
  PAYLOAD_PACK    = settings.pack_tasks разовых решений за settings.pack_price_stars (79)
"""
from aiogram import Bot, F, Router
from aiogram.types import (
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from loguru import logger
from sqlalchemy import text

from app.config import settings
from app.core.db import get_session
from app.ratelimit import activate_premium, add_credits

router = Router()

PAYLOAD_PREMIUM = "academ4i_premium_30d"
PAYLOAD_PACK = "academ4i_pack_5"


async def send_premium_invoice(bot: Bot, chat_id: int) -> None:
    """Премиум-подписка: 30 дней безлимита."""
    await bot.send_invoice(
        chat_id=chat_id,
        title="Academ4I — Premium 30 дней",
        description=(
            "Безлимит решений на 30 дней. Все предметы: матан, линал, "
            "алгебра, группы. AI знает Демидовича и Кострикина."
        ),
        payload=PAYLOAD_PREMIUM,
        currency="XTR",
        prices=[LabeledPrice(label="Premium 30 дней", amount=settings.premium_price_stars)],
    )


async def send_pack_invoice(bot: Bot, chat_id: int) -> None:
    """Пакет N задач без срока (расходуются по мере решений)."""
    await bot.send_invoice(
        chat_id=chat_id,
        title=f"Academ4I — Пакет {settings.pack_tasks} задач",
        description=(
            f"{settings.pack_tasks} решений сверх бесплатных. "
            f"Без срока истечения — используй когда нужно."
        ),
        payload=PAYLOAD_PACK,
        currency="XTR",
        prices=[LabeledPrice(
            label=f"Пакет {settings.pack_tasks} задач",
            amount=settings.pack_price_stars,
        )],
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, bot: Bot):
    """Подтверждение перед списанием. OK для известных payload."""
    if query.invoice_payload in (PAYLOAD_PREMIUM, PAYLOAD_PACK):
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
    """Платёж прошёл — активируем покупку + лог в payments."""
    sp = message.successful_payment
    user_id = message.from_user.id
    payload = sp.invoice_payload

    logger.info(
        f"💎 paid: user={user_id} payload={payload} "
        f"amount={sp.total_amount} {sp.currency} charge_id={sp.telegram_payment_charge_id}"
    )

    if payload == PAYLOAD_PREMIUM:
        premium_until = await activate_premium(user_id, duration_days=settings.premium_duration_days)
        product = "premium_30d"
        result_text = (
            f"✅ <b>Premium активирован!</b>\n\n"
            f"Безлимит решений до <b>{premium_until.strftime('%d.%m.%Y %H:%M UTC')}</b>.\n\n"
            f"Кидай задачи 🎓"
        )
    elif payload == PAYLOAD_PACK:
        new_credits = await add_credits(user_id, settings.pack_tasks)
        product = f"pack_{settings.pack_tasks}"
        premium_until = None
        result_text = (
            f"✅ <b>Пакет {settings.pack_tasks} задач куплен!</b>\n\n"
            f"Доступно решений: <b>{new_credits}</b> (без срока истечения).\n\n"
            f"Кидай задачи 🎓"
        )
    else:
        logger.error(f"successful_payment with unknown payload: {payload}")
        await message.answer("Платёж прошёл, но мы не смогли распознать что куплено. Напиши в @Academ4I_support.")
        return

    # Лог платежа
    try:
        async with get_session() as session:
            sql = """
                INSERT INTO payments (
                    telegram_id, telegram_payment_charge_id, amount_stars,
                    product, premium_from, premium_until, status
                ) VALUES (
                    :tg, :charge, :amt, :product, NOW(),
                    COALESCE(:until, NOW()), 'succeeded'
                ) ON CONFLICT (telegram_payment_charge_id) DO NOTHING
            """
            await session.execute(text(sql), {
                "tg": user_id,
                "charge": sp.telegram_payment_charge_id,
                "amt": sp.total_amount,
                "product": product,
                "until": premium_until,
            })
            await session.commit()
    except Exception as e:
        logger.exception(f"payment log failed (non-fatal): {e}")

    await message.answer(result_text)
