"""Все тексты сообщений бота. HTML parse_mode."""
from app.config import settings


MSG_START = (
    "👋 Привет! Я <b>Academ4I</b> — решаю задачи по матану, линалу и алгебре.\n\n"
    "📸 Кинь <b>фото задачи</b> — получишь решение в виде PDF-картинки + LaTeX-код.\n\n"
    f"🎁 <b>{settings.free_lifetime_tasks} решения бесплатно</b>, дальше — Premium или пакеты "
    "(см. меню под клавиатурой 👇).\n\n"
    "Команды:\n"
    "/help — что я умею\n"
    "/balance — сколько решений осталось\n"
    "/menu — вернуть меню"
)

MSG_HELP = (
    "<b>Что я умею:</b>\n\n"
    "📐 Решаю задачи по:\n"
    "• Математическому анализу (пределы, производные, интегралы, ряды)\n"
    "• Линейной алгебре (матрицы, СЛАУ, векторные пространства)\n"
    "• Общей алгебре (теория групп, кольца, поля, многочлены)\n\n"
    "📸 <b>Как пользоваться:</b>\n"
    "Кидай фото задачи. Если на фото несколько задач — в подписи к фото "
    "укажи номер и подзадачу, например: <i>«реши 2851 а)»</i>.\n\n"
    f"💎 <b>Тарифы:</b>\n"
    f"• Free — <b>{settings.free_lifetime_tasks} решения</b> (попробовать)\n"
    f"• Пакет — <b>{settings.pack_tasks} задач за {settings.pack_price_stars}⭐</b> (без срока)\n"
    f"• Premium — <b>30 дней безлимит за {settings.premium_price_stars}⭐</b>\n\n"
    "Поддержка: @Academ4I_support"
)

MSG_PROCESSING = "📷 Распознаю условие…"

# Подпись под демо-картинкой решения в /start.
MSG_DEMO_CAPTION = (
    "👆 Вот так выглядит решение.\n\n"
    "📸 Просто пришли <b>фото своей задачи</b> — и получишь такое же, "
    "пошагово и с ответом в рамке."
)


def msg_choose_task(task_ids: list[str]) -> str:
    """Текст-вопрос, когда на фото несколько задач и подсказки нет."""
    nums = ", ".join(f"№{t}" for t in task_ids)
    return (
        f"📋 На фото несколько задач: <b>{nums}</b>.\n\n"
        f"Какую решить? Нажми кнопку ниже 👇\n"
        f"<i>(или пришли фото с подписью — например «реши {task_ids[0]} а)»)</i>"
    )

MSG_QUOTA_EXCEEDED = (
    f"⛔ Бесплатные <b>{settings.free_lifetime_tasks} решения</b> закончились.\n\n"
    "Хочешь продолжить — выбери в меню 👇:\n"
    f"• 🎁 <b>{settings.pack_tasks} задач за {settings.pack_price_stars}⭐</b> "
    "(разово, без срока)\n"
    f"• 💎 <b>Premium {settings.premium_price_stars}⭐</b> — безлимит на 30 дней"
)

MSG_ERROR = (
    "😔 Что-то пошло не так. Попробуй ещё раз или пришли <b>более чёткое фото</b>.\n"
    "Если ошибка повторяется — напиши в @Academ4I_support"
)


def msg_balance(quota) -> str:
    """quota — QuotaResult. Текст с балансом юзера."""
    if quota.is_admin:
        return "👑 <b>Админ-аккаунт</b> — безлимит решений."

    if quota.is_premium:
        until = quota.premium_until.strftime("%d.%m.%Y %H:%M UTC") if quota.premium_until else ""
        extra = ""
        if quota.credits > 0:
            extra = f"\n🎁 Пакетных задач сверху: <b>{quota.credits}</b>"
        return (
            f"💎 <b>Premium активен</b>\n"
            f"Безлимит до: <b>{until}</b>{extra}"
        )

    lines = ["📊 <b>Твой баланс</b>", ""]
    if quota.credits > 0:
        lines.append(f"🎁 Купленных задач: <b>{quota.credits}</b>")
    lines.append(
        f"🆓 Бесплатных осталось: <b>{quota.free_remaining}/{settings.free_lifetime_tasks}</b>"
    )
    if quota.total_remaining == 0:
        lines.append("")
        lines.append("Чтобы продолжить — выбери в меню пакет или Premium 👇")
    return "\n".join(lines)


MSG_ADMIN_WELCOME = (
    "👑 <b>Привет, админ!</b> Безлимит решений включён.\n\n"
)


MSG_BUY_PACK_PROMPT = (
    f"🎁 <b>Пакет {settings.pack_tasks} задач</b>\n\n"
    f"✓ {settings.pack_tasks} решений сверх бесплатных\n"
    f"✓ Без срока — используй когда угодно\n"
    f"✓ Цена: <b>{settings.pack_price_stars}⭐</b>\n\n"
    f"Жми кнопку оплаты ниже 👇"
)

MSG_BUY_PREMIUM_PROMPT = (
    f"💎 <b>Premium на 30 дней</b>\n\n"
    f"✓ Безлимит решений\n"
    f"✓ Все темы: матан, линал, алгебра, группы\n"
    f"✓ Цена: <b>{settings.premium_price_stars}⭐</b>\n\n"
    f"Жми кнопку оплаты ниже 👇"
)
