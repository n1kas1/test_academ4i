"""Telegram Stars Subscriptions — 299₽/мес безлимит.

Документация: https://core.telegram.org/bots/api#sendinvoice
            https://core.telegram.org/bots/payments-stars

Поток:
1. Юзер шлёт /subscribe
2. Бот шлёт invoice (sendInvoice с currency='XTR')
3. Юзер платит Stars
4. TG → webhook успешный pre_checkout_query
5. Бот подтверждает (answerPreCheckoutQuery)
6. TG → webhook successful_payment
7. Бот активирует подписку (на 30 дней) в БД

TODO: реализация после готового AI-pipeline
"""
from aiogram import Bot
from aiogram.types import LabeledPrice

from app.config import settings


async def send_subscription_invoice(bot: Bot, chat_id: int):
    """Отправить invoice для подписки 299₽ через TG Stars."""
    await bot.send_invoice(
        chat_id=chat_id,
        title="Academ4I Premium",
        description=(
            "Безлимит решений на 30 дней.\n"
            "• Все предметы (матан, линал, алгебра, теория групп)\n"
            "• Пошаговые решения по учебникам РФ\n"
            "• Отмена в любой момент"
        ),
        payload=f"sub_premium_30d",
        currency="XTR",  # XTR = Telegram Stars
        prices=[LabeledPrice(label="Premium 30 дней", amount=settings.premium_price_stars)],
        # Для подписки: subscription_period в секундах (30 дней = 2592000)
        # TODO: проверить актуальное название параметра в aiogram 3.13+
    )


# TODO:
# async def handle_pre_checkout(query: PreCheckoutQuery): ...
# async def handle_successful_payment(message: Message): ...
# async def activate_premium(user_id: int, duration_days: int): ...
# async def check_subscription(user_id: int) -> bool: ...
