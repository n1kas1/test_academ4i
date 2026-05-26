"""Telegram Stars — покупка пакетов кредитов (credit-модель).

Каждый пакет (см. config.CREDIT_PACKAGES) → начисление credits. Подписок больше
нет. Начисление идемпотентно по telegram_payment_charge_id.
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
from app.config import CreditPackage, PACKAGES_BY_PAYLOAD, settings
from app.core.db import get_session
from app.ratelimit import add_credits, is_admin

router = Router()

_PACKAGES_BY_KEY: dict[str, CreditPackage] = {p.key: p for p in PACKAGES_BY_PAYLOAD.values()}


async def send_credits_invoice(bot: Bot, chat_id: int, pkg: CreditPackage) -> None:
    """Выставить счёт на пакет кредитов."""
    await bot.send_invoice(
        chat_id=chat_id,
        title=f"Academ4I — {pkg.title} ({pkg.credits} кредитов)",
        description=(
            f"{pkg.credits} кредитов без срока. "
            f"Стандарт — {settings.standard_cost} кредит/задача, "
            f"Премиум — {settings.premium_cost} кредитов/задача."
        ),
        payload=pkg.payload,
        currency="XTR",
        prices=[LabeledPrice(label=f"{pkg.credits} кредитов", amount=pkg.stars)],
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, bot: Bot):
    """Подтверждение перед списанием. OK для известных пакетов."""
    if query.invoice_payload in PACKAGES_BY_PAYLOAD:
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
    """Платёж прошёл — начисляем кредиты. Идемпотентно по charge_id.

    Сначала «столбим» платёж (INSERT ... ON CONFLICT DO NOTHING RETURNING id),
    и только если вставка реально произошла — начисляем. Дубль вебхука от
    Telegram второй раз ничего не начислит.
    """
    sp = message.successful_payment
    user_id = message.from_user.id
    payload = sp.invoice_payload
    charge_id = sp.telegram_payment_charge_id

    logger.info(
        f"💎 paid: user={user_id} payload={payload} "
        f"amount={sp.total_amount} {sp.currency} charge_id={charge_id}"
    )

    pkg = PACKAGES_BY_PAYLOAD.get(payload)
    if pkg is None:
        logger.error(f"successful_payment with unknown payload: {payload}")
        await message.answer(
            "Платёж прошёл, но мы не смогли распознать что куплено. "
            "Напиши в @Academ4I_support."
        )
        return

    new_credits = None
    duplicate = False

    async with get_session() as session:
        # 1) Идемпотентный «захват» платежа по уникальному charge_id.
        #    premium_from/until — NOT NULL в схеме (наследие подписок); для пакетов
        #    кладём NOW() как заглушку, чтобы не менять схему.
        claim = await session.execute(text("""
            INSERT INTO payments (
                telegram_id, telegram_payment_charge_id, amount_stars,
                product, premium_from, premium_until, status
            ) VALUES (:tg, :charge, :amt, :product, NOW(), NOW(), 'processing')
            ON CONFLICT (telegram_payment_charge_id) DO NOTHING
            RETURNING id
        """), {"tg": user_id, "charge": charge_id, "amt": sp.total_amount, "product": pkg.key})
        claimed = claim.first()

        if claimed is None:
            duplicate = True
        else:
            payment_id = claimed[0]
            # 2) Начисление кредитов в той же транзакции, что и запись платежа.
            new_credits = await add_credits(user_id, pkg.credits, session=session)
            await session.execute(text(
                "UPDATE payments SET status = 'succeeded' WHERE id = :id"
            ), {"id": payment_id})
            await session.commit()

    if duplicate:
        logger.warning(f"Duplicate successful_payment ignored: user={user_id} charge_id={charge_id}")
        await message.answer(
            "✅ Этот платёж уже был активирован ранее — повторно начислять не нужно.",
            reply_markup=main_menu_keyboard(is_admin=is_admin(message.from_user.username)),
        )
        return

    await message.answer(
        f"✅ <b>Пакет {pkg.title} куплен!</b>\n\n"
        f"Начислено <b>{pkg.credits}</b> кредитов. Баланс: <b>{new_credits}</b> (без срока).\n\n"
        f"Кидай задачи 🎓",
        reply_markup=main_menu_keyboard(is_admin=is_admin(message.from_user.username)),
    )


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy_package(callback: CallbackQuery, bot: Bot):
    """Выбор пакета из inline-меню (buy:<key>)."""
    await callback.answer()
    key = callback.data.split(":", 1)[1]
    pkg = _PACKAGES_BY_KEY.get(key)
    if pkg is None:
        await callback.message.answer("Пакет не найден, открой меню заново.")
        return
    await send_credits_invoice(bot, callback.message.chat.id, pkg)
