"""Все тексты сообщений бота. HTML parse_mode. Credit-модель."""
from app.config import settings, CREDIT_PACKAGES


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


MSG_ERROR = (
    "😔 Что-то пошло не так. Попробуй ещё раз или пришли <b>более чёткое фото</b>.\n"
    "Если ошибка повторяется — напиши в @Academ4I_support"
)


MSG_ADMIN_WELCOME = (
    "👑 <b>Привет, админ!</b> Безлимит решений включён.\n\n"
)

MSG_ADMIN_HELP = (
    "🛠 <b>Команды админа</b>\n"
    "/admin — панель управления\n"
    "/stats — статистика и воронка\n"
    "/broadcast &lt;текст&gt; — рассылка всем юзерам"
)


# ════════════════════════════════════════════════════════════════════════
# Credit-based pricing
# ════════════════════════════════════════════════════════════════════════

_PKG_LINES = "\n".join(
    f"• <b>{p.title}</b> — {p.credits} кредитов за {p.stars}⭐" for p in CREDIT_PACKAGES
)

MSG_START_CREDITS = (
    "👋 Привет! Я <b>Academ4I</b> — решаю задачи по матану, линалу, алгебре, "
    "теорверу и дискретной математике.\n\n"
    "📸 Кинь <b>фото задачи</b> — выберешь режим и получишь решение (PDF + LaTeX).\n\n"
    f"🎁 Тебе начислено <b>{settings.trial_credits} кредитов</b> на старт.\n"
    f"⚡ <b>Стандарт</b> — {settings.standard_cost} кредит/задача\n"
    f"💎 <b>Премиум</b> — {settings.premium_cost} кредитов/задача (Sonnet + рассуждения)\n\n"
    "Команды:\n"
    "/help — что я умею\n"
    "/balance — баланс кредитов\n"
    "/menu — вернуть меню"
)

MSG_HELP_CREDITS = (
    "<b>Что я умею:</b>\n\n"
    "📐 Матан, линал, общая алгебра, теорвер, дискретка.\n\n"
    "📸 <b>Как пользоваться:</b> кидай фото задачи, выбирай режим. "
    "Если на фото несколько задач — в подписи укажи номер (например <i>«реши 2851 а)»</i>).\n\n"
    "💳 <b>Кредиты:</b>\n"
    f"• ⚡ Стандарт — <b>{settings.standard_cost} кредит</b> (быстро, типовые задачи)\n"
    f"• 💎 Премиум — <b>{settings.premium_cost} кредитов</b> (сложное, доказательства)\n\n"
    f"<b>Пакеты:</b>\n{_PKG_LINES}\n\n"
    "Поддержка: @Academ4I_support"
)

MSG_BUY_CREDITS_PROMPT = (
    "💳 <b>Пакеты кредитов</b> (без срока):\n\n"
    f"{_PKG_LINES}\n\n"
    f"⚡ Стандарт = {settings.standard_cost} кредит · 💎 Премиум = {settings.premium_cost} кредитов.\n"
    "Выбери пакет ниже 👇"
)

MSG_OCR_FAILED_STANDARD = (
    "😕 Не удалось разобрать условие с фото в режиме <b>Стандарт</b>.\n\n"
    "Переснимите чётче или выберите 💎 <b>Премиум</b> (распознаёт прямо с фото).\n"
    "Кредиты не списаны."
)


def msg_mode_prompt(status) -> str:
    """status — CreditStatus. Предложение выбрать режим с показом баланса."""
    bal = "∞ (админ)" if status.is_admin else f"{status.credits}"
    return (
        "Выбери режим решения 👇\n\n"
        f"⚡ <b>Стандарт</b> — {settings.standard_cost} кредит (быстро, типовые задачи)\n"
        f"💎 <b>Премиум</b> — {settings.premium_cost} кредитов (Sonnet + рассуждения, "
        "для сложного и доказательств)\n\n"
        f"💰 Баланс: <b>{bal}</b> кредитов"
    )


def msg_balance_credits(status) -> str:
    """status — CreditStatus."""
    if status.is_admin:
        return "👑 <b>Админ-аккаунт</b> — безлимит решений."
    lines = [
        f"💰 <b>Баланс: {status.credits} кредитов</b>",
        "",
        f"⚡ Стандарт — {settings.standard_cost}/задача · 💎 Премиум — {settings.premium_cost}/задача",
    ]
    if status.credits <= 0:
        lines += ["", "Пополни пакетом в меню 👇"]
    return "\n".join(lines)


def msg_insufficient_credits(credits: int, cost: int) -> str:
    """Paywall: не хватает кредитов на выбранный режим."""
    return (
        f"⛔ Не хватает кредитов: нужно <b>{cost}</b>, у тебя <b>{credits}</b>.\n\n"
        "Пополни пакетом 👇 (выбери в меню «Пакеты кредитов»)."
    )
