"""Клавиатуры: persistent reply (главное меню) + inline (под решениями)."""
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.config import settings, CREDIT_PACKAGES

# === Тексты кнопок главного меню ===
BTN_BUY_CREDITS = "💳 Пакеты кредитов"
BTN_BALANCE = "📊 Мой баланс"
BTN_HELP = "ℹ️ Помощь"


def main_menu_keyboard(is_premium: bool = False, is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Главное меню (credit-модель). Админ → без кнопки покупки.

    Параметр is_premium сохранён для совместимости вызовов, в credit-модели не влияет.
    """
    if is_admin:
        keyboard = [[KeyboardButton(text=BTN_BALANCE), KeyboardButton(text=BTN_HELP)]]
        placeholder = "📸 Кинь фото задачи (админ — безлимит)"
    else:
        keyboard = [
            [KeyboardButton(text=BTN_BUY_CREDITS)],
            [KeyboardButton(text=BTN_BALANCE), KeyboardButton(text=BTN_HELP)],
        ]
        placeholder = "📸 Кинь фото задачи или выбери из меню"

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder=placeholder,
        is_persistent=True,
    )


def packages_keyboard() -> InlineKeyboardMarkup:
    """Inline-выбор пакета кредитов (callback buy:<key>)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{p.title} — {p.credits} кредитов · {p.stars}⭐",
            callback_data=f"buy:{p.key}",
        )]
        for p in CREDIT_PACKAGES
    ])


def mode_choice_keyboard(token: str) -> InlineKeyboardMarkup:
    """Inline-выбор режима перед решением (callback mode:<token>:<standard|premium>)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"⚡ Стандарт · {settings.standard_cost} кредит",
            callback_data=f"mode:{token}:standard",
        )],
        [InlineKeyboardButton(
            text=f"💎 Премиум · {settings.premium_cost} кредитов",
            callback_data=f"mode:{token}:premium",
        )],
    ])


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
            text="🔄 Перерешать",
            callback_data=f"resolve:{token}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
