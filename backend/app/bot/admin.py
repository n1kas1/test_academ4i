"""Админ-команды: /stats (метрики) и /broadcast (рассылка всем). Под is_admin."""
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import text

from app.core.db import get_session
from app.core.redis import get_redis
from app.notify import broadcast_send
from app.ratelimit import is_admin

router = Router()

_BROADCAST_TTL_SEC = 600  # черновик рассылки живёт 10 минут


def _broadcast_key(admin_id: int) -> str:
    return f"broadcast:draft:{admin_id}"


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


_STATS_SQL = text("""
SELECT
  (SELECT COUNT(*) FROM users) AS total_users,
  (SELECT COUNT(*) FROM users WHERE created_at > now() - interval '1 day') AS new_1d,
  (SELECT COUNT(*) FROM users WHERE created_at > now() - interval '7 days') AS new_7d,
  (SELECT COUNT(*) FROM users WHERE created_at > now() - interval '30 days') AS new_30d,
  (SELECT COUNT(DISTINCT telegram_id) FROM events WHERE created_at > now() - interval '1 day') AS dau,
  (SELECT COUNT(DISTINCT telegram_id) FROM events WHERE created_at > now() - interval '7 days') AS wau,
  (SELECT COUNT(*) FROM events WHERE type='solve' AND created_at > now() - interval '7 days') AS solved_7d,
  (SELECT COUNT(*) FROM events WHERE type='solve' AND created_at > now() - interval '30 days') AS solved_30d,
  (SELECT COUNT(DISTINCT telegram_id) FROM events WHERE type='start') AS f_start,
  (SELECT COUNT(DISTINCT telegram_id) FROM events WHERE type='solve') AS f_solve,
  (SELECT COUNT(DISTINCT telegram_id) FROM events WHERE type='paywall_shown') AS f_paywall,
  (SELECT COUNT(DISTINCT telegram_id) FROM payments WHERE status='succeeded') AS f_purchase,
  (SELECT COALESCE(SUM(amount_stars), 0) FROM payments WHERE status='succeeded'
     AND created_at > now() - interval '30 days') AS revenue_30d
""")

_PRODUCTS_SQL = text(
    "SELECT product, COUNT(*) AS n FROM payments WHERE status='succeeded' GROUP BY product"
)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.username):
        return
    async with get_session() as session:
        r = (await session.execute(_STATS_SQL)).one()._mapping
        products = (await session.execute(_PRODUCTS_SQL)).all()

    prod_lines = "\n".join(f"  • {p}: {n}" for p, n in products) or "  • —"

    report = (
        "📊 <b>Статистика</b>\n\n"
        "<b>Аудитория</b>\n"
        f"Всего юзеров: <b>{r['total_users']}</b>\n"
        f"Новые: {r['new_1d']} / сут · {r['new_7d']} / нед · {r['new_30d']} / мес\n"
        f"DAU: <b>{r['dau']}</b> · WAU: <b>{r['wau']}</b>\n"
        f"Решено задач: {r['solved_7d']} / нед · {r['solved_30d']} / мес\n\n"
        "<b>Воронка</b> (за всё время)\n"
        f"1. Старт: <b>{r['f_start']}</b>\n"
        f"2. Решили ≥1: <b>{r['f_solve']}</b> ({_pct(r['f_solve'], r['f_start'])})\n"
        f"3. Упёрлись в paywall: <b>{r['f_paywall']}</b> ({_pct(r['f_paywall'], r['f_solve'])})\n"
        f"4. Купили: <b>{r['f_purchase']}</b> ({_pct(r['f_purchase'], r['f_paywall'])})\n\n"
        "<b>Деньги</b>\n"
        f"Выручка за 30 дн: <b>{r['revenue_30d']}⭐</b>\n"
        f"Покупки по продуктам:\n{prod_lines}"
    )
    await message.answer(report)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if not is_admin(message.from_user.username):
        return
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
    if not is_admin(callback.from_user.username):
        await callback.answer()
        return
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
    sent, failed = await broadcast_send(bot, ids, body)
    await callback.message.answer(f"✅ Готово: отправлено <b>{sent}</b>, не дошло <b>{failed}</b>.")


@router.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery):
    if not is_admin(callback.from_user.username):
        await callback.answer()
        return
    await get_redis().delete(_broadcast_key(callback.from_user.id))
    await callback.answer()
    await callback.message.edit_text("❌ Рассылка отменена.")
