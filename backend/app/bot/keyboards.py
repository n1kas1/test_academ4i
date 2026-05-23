"""Клавиатуры: persistent reply (главное меню) + inline (под решениями)."""
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.config import settings

# === Тексты кнопок главного меню ===
BTN_BUY_PACK = "🎁 Пакеты задач"
BTN_BUY_PREMIUM = "💎 Premium"
BTN_BALANCE = "📊 Мой баланс"
BTN_HELP = "ℹ️ Помощь"


def pack_choice_keyboard() -> InlineKeyboardMarkup:
    """Inline-выбор пакета задач (без срока)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🎁 {settings.pack_tasks} задач — {settings.pack_price_stars}⭐",
            callback_data="buy:pack5",
        )],
        [InlineKeyboardButton(
            text=f"🎁 {settings.pack_large_tasks} задач — {settings.pack_large_price_stars}⭐",
            callback_data="buy:pack10",
        )],
    ])


def premium_choice_keyboard() -> InlineKeyboardMarkup:
    """Inline-выбор периода Premium."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💎 Неделя — {settings.premium_week_price_stars}⭐",
            callback_data="buy:premweek",
        )],
        [InlineKeyboardButton(
            text=f"💎 Месяц — {settings.premium_price_stars}⭐",
            callback_data="buy:premmonth",
        )],
    ])


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


def solution_keyboard(token: str, allow_resolve: bool = True) -> InlineKeyboardMarkup:
    """Inline под решением: показать LaTeX + (опц.) перерешать.

    Оба действия на одном token (данные решения лежат в Redis под sol:{token}).
    allow_resolve=False — для результата самого «перерешать» (одна бесплатная
    попытка на решение, чтобы не было бесконечной цепочки).
    """
    rows = [[InlineKeyboardButton(
        text="📋 Показать LaTeX (для копирования)",
        callback_data=f"latex:{token}",
    )]]
    if allow_resolve:
        rows.append([InlineKeyboardButton(
            text="🔄 Перерешать (если ответ неверный)",
            callback_data=f"resolve:{token}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def renew_premium_keyboard() -> InlineKeyboardMarkup:
    """Inline-кнопка под уведомлением об окончании Premium."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💎 Продлить Premium", callback_data="renew_premium"),
    ]])


def task_choice_keyboard(token: str, task_ids: list[str]) -> InlineKeyboardMarkup:
    """Inline-выбор: какую из нескольких задач на фото решить.

    callback_data = "pick:{token}:{index}" — index указывает на task_ids[index],
    сами номера хранятся в Redis под token (в callback_data не влезут много).
    """
    rows = []
    row = []
    for i, tid in enumerate(task_ids):
        row.append(InlineKeyboardButton(text=f"№{tid}", callback_data=f"pick:{token}:{i}"))
        if len(row) == 3:          # по 3 кнопки в ряд
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)
