"""Клавиатуры: persistent reply (главное меню) + inline (под решениями)."""
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.config import settings

# === Тексты кнопок главного меню ===
BTN_BUY_PACK = f"🎁 {settings.pack_tasks} задач — {settings.pack_price_stars}⭐"
BTN_BUY_PREMIUM = f"💎 Premium 30 дней — {settings.premium_price_stars}⭐"
BTN_BALANCE = "📊 Мой баланс"
BTN_HELP = "ℹ️ Помощь"


def main_menu_keyboard(is_premium: bool = False, is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Главное меню. Если у юзера активен Premium / он админ — кнопки покупки скрыты."""
    if is_premium or is_admin:
        keyboard = [[KeyboardButton(text=BTN_BALANCE), KeyboardButton(text=BTN_HELP)]]
        placeholder = "📸 Кинь фото задачи (безлимит активен)"
    else:
        keyboard = [
            [KeyboardButton(text=BTN_BUY_PACK), KeyboardButton(text=BTN_BUY_PREMIUM)],
            [KeyboardButton(text=BTN_BALANCE), KeyboardButton(text=BTN_HELP)],
        ]
        placeholder = "📸 Кинь фото задачи или выбери из меню"

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder=placeholder,
        is_persistent=True,
    )


def latex_view_keyboard(latex_token: str) -> InlineKeyboardMarkup:
    """Inline под PNG-решением."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📋 Показать LaTeX (для копирования)",
            callback_data=f"latex:{latex_token}",
        ),
    ]])
