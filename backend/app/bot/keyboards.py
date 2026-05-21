"""Клавиатуры: persistent reply (главное меню) + inline (под решениями)."""
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.config import settings

# === Кнопки главного меню (persistent под клавиатурой) ===
BTN_BUY_PACK = f"🎁 {settings.pack_tasks} задач — {settings.pack_price_stars}⭐"
BTN_BUY_PREMIUM = f"💎 Premium 30 дней — {settings.premium_price_stars}⭐"
BTN_BALANCE = "📊 Мой баланс"
BTN_HELP = "ℹ️ Помощь"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню — всегда под клавиатурой ввода."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BUY_PACK), KeyboardButton(text=BTN_BUY_PREMIUM)],
            [KeyboardButton(text=BTN_BALANCE), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="📸 Кинь фото задачи или выбери из меню",
        is_persistent=True,
    )


def latex_view_keyboard(latex_token: str) -> InlineKeyboardMarkup:
    """Inline под PNG: открыть LaTeX-код."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📋 Показать LaTeX (для копирования)",
            callback_data=f"latex:{latex_token}",
        ),
    ]])
