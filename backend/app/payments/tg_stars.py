"""Telegram Stars — оплата Premium-подписки за 200 Stars (~399₽).

Flow:
1. Юзер /subscribe → бот шлёт invoice (sendInvoice, currency='XTR').
2. Юзер тапает оплату → Telegram запрашивает у нашего бота pre_checkout_query.
3. Мы валидируем (всегда OK для известного payload) → answer_pre_checkout_query(ok=True).
4. Юзер подтверждает → Telegram шлёт message с successful_payment.
5. Активируем Premium на 30 дней в БД.
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
from app.ratelimit import activate_premium

router = Router()

PAYLOAD_PREMIUM = "academ4i_premium_30d"


async def send_subscription_invoice(bot: Bot, chat_id: int) -> None:
    """Отправить invoice для Premium-подписки через TG Stars."""
    prices = [
        LabeledPrice(
            label="Premium на 30 дней",
            amount=settings.premium_price_stars,  # в Stars (XTR)
        )
    ]
    await bot.send_invoice(
        chat_id=chat_id,
        title="Academ4I Premium",
        description=(
            "30 дней безлимита решений по матану, линалу и алгебре.\n"
            "AI знает Демидовича и Кострикина наизусть."
        ),
        payload=PAYLOAD_PREMIUM,
        currency="XTR",  # XTR — Telegram Stars
        prices=prices,
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, bot: Bot):
    """Подтверждение перед списанием. Всегда ok=True для нашего payload."""
    if query.invoice_payload == PAYLOAD_PREMIUM:
        logger.info(f"pre_checkout OK: user={query.from_user.id} amount={query.total_amount}")
        await bot.answer_pre_checkout_query(query.id, ok=True)
    else:
        logger.warning(f"pre_checkout REJECT: unknown payload {query.invoice_payload!r}")
        await bot.answer_pre_checkout_query(
            query.id,
            ok=False,
            error_message="Неизвестный платёж. Попробуй /subscribe заново.",
        )


@router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    """Платёж прошёл — активируем Premium на 30 дней + лог в payments."""
    sp = message.successful_payment
    user_id = message.from_user.id
    logger.info(
        f"💎 successful_payment: user={user_id} "
        f"amount={sp.total_amount} {sp.currency} "
        f"charge_id={sp.telegram_payment_charge_id}"
    )

    # Активируем Premium и сохраним лог платежа
    premium_until = await activate_premium(user_id, duration_days=settings.premium_duration_days)

    try:
        async with get_session() as session:
            sql = """
                INSERT INTO payments (
                    telegram_id, telegram_payment_charge_id, amount_stars,
                    product, premium_from, premium_until, status
                ) VALUES (
                    :tg, :charge, :amt, :product, NOW(), :until, 'succeeded'
                ) ON CONFLICT (telegram_payment_charge_id) DO NOTHING
            """
            await session.execute(text(sql), {
                "tg": user_id,
                "charge": sp.telegram_payment_charge_id,
                "amt": sp.total_amount,
                "product": "premium_30d",
                "until": premium_until,
            })
            await session.commit()
    except Exception as e:
        logger.exception(f"payment log failed (non-fatal): {e}")

    from app.bot.messages import MSG_PAYMENT_SUCCESS
    await message.answer(MSG_PAYMENT_SUCCESS)
