"""Админ-команды и панель: /admin (меню), /stats (метрики), /broadcast (рассылка).

Всё под is_admin. /stats и админ-меню работают на инлайн-кнопках:
- меню: 📊 Статистика / 📢 Рассылка
- статистика: переключатель периода (сегодня / 7 / 30 дней), пересчёт по кнопке
"""
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, Filter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import text

from app.core.background import spawn
from app.core.db import get_session
from app.core.redis import get_redis
from app.notify import broadcast_send
from app.ratelimit import is_admin


class IsAdmin(Filter):
    """Пускает только админов. Не-админ → апдейт уходит дальше по роутерам."""
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user and is_admin(event.from_user.username))


router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

_BROADCAST_TTL_SEC = 600  # черновик рассылки живёт 10 минут

# Периоды переключателя статистики: дни → подпись.
_PERIODS = ((1, "сегодня"), (7, "7 дней"), (30, "30 дней"))


def _broadcast_key(admin_id: int) -> str:
    return f"broadcast:draft:{admin_id}"


def _pct_suffix(part: int, whole: int) -> str:
    """' (NN%)' от предыдущего шага воронки; пусто, если делить не на что.

    Клампим до 100%: когорты пересекаются — событие start логируется только с
    момента деплоя аналитики, поэтому «решивших» может оказаться больше «стартовавших».
    """
    if not whole:
        return ""
    return f" ({min(100, round(100 * part / whole))}%)"


# === Клавиатуры ===

def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
        InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast"),
    ]])


def _stats_kb(active_days: int) -> InlineKeyboardMarkup:
    period_row = [
        InlineKeyboardButton(
            text=(f"• {label} •" if days == active_days else label),
            callback_data=f"stats:{days}",
        )
        for days, label in _PERIODS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        period_row,
        [InlineKeyboardButton(text="⬅️ Меню", callback_data="admin:menu")],
    ])


# === Статистика ===

_STATS_SQL = text("""
SELECT
  (SELECT COUNT(*) FROM users) AS total_users,
  (SELECT COUNT(*) FROM users WHERE created_at > now() - make_interval(days => :days)) AS new_p,
  (SELECT COUNT(DISTINCT telegram_id) FROM events WHERE created_at > now() - make_interval(days => :days)) AS active_p,
  (SELECT COUNT(*) FROM events WHERE type='solve' AND created_at > now() - make_interval(days => :days)) AS solved_p,
  (SELECT COUNT(DISTINCT telegram_id) FROM events WHERE type='start') AS f_start,
  (SELECT COUNT(DISTINCT telegram_id) FROM events WHERE type='solve') AS f_solve,
  (SELECT COUNT(DISTINCT telegram_id) FROM events WHERE type='paywall_shown') AS f_paywall,
  (SELECT COUNT(DISTINCT telegram_id) FROM payments WHERE status='succeeded') AS f_purchase,
  (SELECT COALESCE(SUM(amount_stars), 0) FROM payments WHERE status='succeeded'
     AND created_at > now() - make_interval(days => :days)) AS revenue_p
""")

_PRODUCTS_SQL = text(
    "SELECT product, COUNT(*) AS n FROM payments WHERE status='succeeded' GROUP BY product"
)


async def _render_stats(days: int) -> str:
    label = next((lbl for d, lbl in _PERIODS if d == days), f"{days} дн")
    async with get_session() as session:
        r = (await session.execute(_STATS_SQL, {"days": days})).one()._mapping
        products = (await session.execute(_PRODUCTS_SQL)).all()

    prod_lines = "\n".join(f"   • {p}: {n}" for p, n in products) or "   • —"

    return (
        f"📊 <b>Статистика</b> · <i>{label}</i>\n"
        "━━━━━━━━━━━━━━\n"
        "👥 <b>Аудитория</b>\n"
        f"Всего юзеров: <b>{r['total_users']}</b>\n"
        f"Новых: <b>{r['new_p']}</b> · активных: <b>{r['active_p']}</b>\n"
        f"Решено задач: <b>{r['solved_p']}</b>\n\n"
        "🛒 <b>Воронка</b> <i>(за всё время)</i>\n"
        f"🟢 Старт — <b>{r['f_start']}</b>\n"
        f"✏️ Решили ≥1 — <b>{r['f_solve']}</b>{_pct_suffix(r['f_solve'], r['f_start'])}\n"
        f"⛔ Дошли до paywall — <b>{r['f_paywall']}</b>{_pct_suffix(r['f_paywall'], r['f_solve'])}\n"
        f"💎 Купили — <b>{r['f_purchase']}</b>{_pct_suffix(r['f_purchase'], r['f_paywall'])}\n\n"
        "💰 <b>Деньги</b>\n"
        f"Выручка за период: <b>{r['revenue_p']}⭐</b>\n"
        f"Покупки (всё время):\n{prod_lines}"
    )


# === Команды и колбэки ===

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    await message.answer("🛠 <b>Админ-панель</b>\nВыбери раздел 👇", reply_markup=_menu_kb())


@router.callback_query(F.data == "admin:menu")
async def admin_menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "🛠 <b>Админ-панель</b>\nВыбери раздел 👇", reply_markup=_menu_kb()
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    await message.answer(await _render_stats(7), reply_markup=_stats_kb(7))


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(await _render_stats(7), reply_markup=_stats_kb(7))


@router.callback_query(F.data.startswith("stats:"))
async def stats_period(callback: CallbackQuery):
    try:
        days = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        days = 7
    await callback.answer()
    try:
        await callback.message.edit_text(await _render_stats(days), reply_markup=_stats_kb(days))
    except TelegramBadRequest:
        pass  # «message is not modified» при повторном тапе того же периода — игнор


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_hint(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "📢 Чтобы разослать сообщение всем юзерам, пришли:\n"
        "<code>/broadcast текст сообщения</code>"
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    body = message.text.partition(" ")[2].strip()
    if not body:
        await message.answer("Формат: <code>/broadcast текст сообщения</code>")
        return
    await get_redis().set(_broadcast_key(message.from_user.id), body, ex=_BROADCAST_TTL_SEC)
    async with get_session() as session:
        n = (await session.execute(text("SELECT COUNT(*) FROM users"))).scalar_one()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Отправить ({n})", callback_data="broadcast:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel"),
    ]])
    await message.answer(f"📢 <b>Превью:</b>\n\n{body}\n\nОтправить {n} юзерам?", reply_markup=kb)


@router.callback_query(F.data == "broadcast:confirm")
async def broadcast_confirm(callback: CallbackQuery, bot: Bot):
    redis = get_redis()
    key = _broadcast_key(callback.from_user.id)
    body = await redis.get(key)
    if not body:
        await callback.answer("Черновик истёк — повтори /broadcast", show_alert=True)
        return
    await redis.delete(key)
    await callback.answer()
    await callback.message.edit_text("📤 Рассылка пошла…")
    async with get_session() as session:
        rows = (await session.execute(text("SELECT telegram_id FROM users"))).all()
    ids = [row[0] for row in rows]
    # В фоне: рассылка может идти десятки секунд и не должна держать ответ вебхука.
    spawn(_run_broadcast(bot, ids, body, callback.message))


async def _run_broadcast(bot: Bot, ids: list[int], body: str, status_message: Message) -> None:
    sent, failed = await broadcast_send(bot, ids, body)
    await status_message.answer(f"✅ Готово: отправлено <b>{sent}</b>, не дошло <b>{failed}</b>.")


@router.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery):
    await get_redis().delete(_broadcast_key(callback.from_user.id))
    await callback.answer()
    await callback.message.edit_text("❌ Рассылка отменена.")
