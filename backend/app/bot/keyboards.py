"""Inline-клавиатуры для бота."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def latex_view_keyboard(latex_token: str) -> InlineKeyboardMarkup:
    """Кнопка под PNG-решением: показать сырой LaTeX для копирования."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📋 Показать LaTeX (для копирования)",
            callback_data=f"latex:{latex_token}",
        ),
    ]])
