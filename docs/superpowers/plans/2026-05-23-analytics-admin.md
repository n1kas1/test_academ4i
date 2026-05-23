# Event Analytics + Admin Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Append-only event log in Postgres plus in-bot admin commands `/stats` (audience + conversion funnel) and `/broadcast` (send-to-all), built on a shared throttled sender.

**Architecture:** New `events` table written via a fire-and-forget `log_event` helper instrumented at `start` / `solve` / `paywall_shown`. A new `app/notify.py` extracts the flood-aware send primitive (currently inlined in `premium_notify.py`) so both premium reminders and broadcasts reuse it. A new admin router (`app/bot/admin.py`) runs aggregate SQL for `/stats` and a two-step confirmed `/broadcast`.

**Tech Stack:** Python 3.12, aiogram 3.13, SQLAlchemy async + asyncpg, Alembic, Redis, Postgres (Supabase). Runs in Docker.

**Testing note:** This repo has no test harness (Docker-only, TeX Live/poppler deps; per CLAUDE.md smoke-tests go through the bot). Per the spec, a pytest harness is explicitly out of scope. Each task therefore verifies with `py_compile`, an Alembic migration run, and a defined bot smoke-test instead of unit tests.

**Deviations from spec (intentional, YAGNI):**
- `solve` event is logged with **no props**. The pipeline `result` dict only exposes `{latex, png, pdf}` (see `app/ai/pipeline.py:136,176,223`); `topic`/`cache_hit` would require editing the core pipeline and are not needed for the chosen metrics (audience + funnel). Deferred.
- `purchase` is read from the `payments` table (authoritative), not duplicated into `events`.

---

## File Structure

New files:
- `backend/app/models/event.py` — `Event` ORM model.
- `backend/alembic/versions/0003_events.py` — migration creating `events`.
- `backend/app/notify.py` — `send_one` + `broadcast_send` (shared throttled sender).
- `backend/app/analytics.py` — `log_event` fire-and-forget logger.
- `backend/app/bot/admin.py` — admin router: `/stats`, `/broadcast` + confirm/cancel callbacks.

Modified files:
- `backend/app/models/__init__.py` — register `Event`.
- `backend/app/premium_notify.py` — use shared `send_one`/`SEND_DELAY_SEC` from `notify.py`.
- `backend/app/bot/handlers.py` — 4 `log_event` calls + import.
- `backend/app/main.py` — `dp.include_router(admin.router)` before `handlers.router`.

---

## Task 1: Event model + registration + migration

**Files:**
- Create: `backend/app/models/event.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/0003_events.py`

- [ ] **Step 1: Create the Event model**

`backend/app/models/event.py`:
```python
"""Event — append-only лог продуктовых событий для аналитики."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    props: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (Index("ix_events_type_created", "type", "created_at"),)
```

- [ ] **Step 2: Register Event in models package**

In `backend/app/models/__init__.py`, add the import and `__all__` entry:
```python
from app.models.base import Base
from app.models.user import User
from app.models.solution import Solution
from app.models.payment import Payment
from app.models.event import Event

__all__ = ["Base", "User", "Solution", "Payment", "Event"]
```

- [ ] **Step 3: Create the migration**

`backend/alembic/versions/0003_events.py`:
```python
"""Add events table (product analytics log)

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("props", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_events_telegram_id", "events", ["telegram_id"])
    op.create_index("ix_events_created_at", "events", ["created_at"])
    op.create_index("ix_events_type_created", "events", ["type", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_events_type_created", table_name="events")
    op.drop_index("ix_events_created_at", table_name="events")
    op.drop_index("ix_events_telegram_id", table_name="events")
    op.drop_table("events")
```

- [ ] **Step 4: Syntax-check**

Run: `python3 -m py_compile backend/app/models/event.py backend/app/models/__init__.py backend/alembic/versions/0003_events.py`
Expected: no output (exit 0).

- [ ] **Step 5: Apply the migration (Docker)**

Run: `docker compose run --rm backend alembic upgrade head`
Expected: `Running upgrade 0002 -> 0003`. If a prepared-statement error appears (Supabase Transaction pooler), re-run via Session pooler per DEPLOY.md step 4 (`DATABASE_URL=...:5432... docker compose run --rm -e DATABASE_URL backend alembic upgrade head`).

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/event.py backend/app/models/__init__.py backend/alembic/versions/0003_events.py
git commit -m "feat: events table for product analytics"
```

---

## Task 2: Shared throttled sender + premium_notify refactor

**Files:**
- Create: `backend/app/notify.py`
- Modify: `backend/app/premium_notify.py`

- [ ] **Step 1: Create the shared sender**

`backend/app/notify.py`:
```python
"""Общий троттлящий рассыльщик: учитывает Telegram flood-control (429)."""
import asyncio
from typing import Iterable, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from loguru import logger

SEND_DELAY_SEC = 0.05  # ~20 msg/s — держим темп ниже flood-лимита Telegram


async def send_one(
    bot: Bot,
    tg: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    """Отправить одно сообщение. True при успехе.

    Flood-control (429) — переждать retry_after и повторить один раз. Прочие
    ошибки (юзер заблокировал бота и т.п.) — пропустить, не валя всю рассылку.
    """
    for _ in range(2):
        try:
            await bot.send_message(tg, text, reply_markup=reply_markup)
            return True
        except TelegramRetryAfter as e:
            logger.warning(f"send flood: sleeping {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            logger.warning(f"send skip {tg}: {e}")
            return False
    return False


async def broadcast_send(
    bot: Bot,
    telegram_ids: Iterable[int],
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> tuple[int, int]:
    """Разослать text всем telegram_ids с пейсингом. Возвращает (sent, failed)."""
    sent = 0
    failed = 0
    for tg in telegram_ids:
        if await send_one(bot, tg, text, reply_markup):
            sent += 1
            await asyncio.sleep(SEND_DELAY_SEC)
        else:
            failed += 1
    return sent, failed
```

- [ ] **Step 2: Refactor premium_notify to use the shared primitive**

In `backend/app/premium_notify.py`:

(a) Replace the aiogram-exception import line `from aiogram.exceptions import TelegramRetryAfter` with:
```python
from app.notify import SEND_DELAY_SEC, send_one
```

(b) Delete the `_SEND_DELAY_SEC` constant line and the entire `_send_once` function.

(c) In `_notify`, replace the send block:
```python
        if await _send_once(bot, tg, text_msg):
            await redis.set(key, "1", ex=_DEDUP_TTL_SEC)  # помечаем только при успехе
            sent += 1
            await asyncio.sleep(_SEND_DELAY_SEC)
```
with:
```python
        if await send_one(bot, tg, text_msg, reply_markup=renew_premium_keyboard()):
            await redis.set(key, "1", ex=_DEDUP_TTL_SEC)  # помечаем только при успехе
            sent += 1
            await asyncio.sleep(SEND_DELAY_SEC)
```

(d) Confirm `from aiogram import Bot` and `from app.bot.keyboards import renew_premium_keyboard` imports remain (they do). The `import asyncio` line stays (still used for `sleep`/`CancelledError`).

- [ ] **Step 3: Syntax-check**

Run: `python3 -m py_compile backend/app/notify.py backend/app/premium_notify.py`
Expected: no output (exit 0).

- [ ] **Step 4: Commit**

```bash
git add backend/app/notify.py backend/app/premium_notify.py
git commit -m "refactor: extract shared throttled sender into app/notify.py"
```

---

## Task 3: Analytics logger + instrument handlers

**Files:**
- Create: `backend/app/analytics.py`
- Modify: `backend/app/bot/handlers.py`

- [ ] **Step 1: Create the analytics logger**

`backend/app/analytics.py`:
```python
"""Лёгкий лог продуктовых событий. Fire-and-forget — не влияет на UX и hot-path."""
import asyncio
import json

from loguru import logger
from sqlalchemy import text

from app.core.db import get_session

# Держим ссылки на фоновые задачи, чтобы их не собрал GC до завершения.
_background: set[asyncio.Task] = set()


async def _insert_event(telegram_id: int, event_type: str, props: dict | None) -> None:
    try:
        props_json = json.dumps(props) if props else None
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO events (id, telegram_id, type, props) "
                    "VALUES (gen_random_uuid(), :tg, :type, CAST(:props AS jsonb))"
                ),
                {"tg": telegram_id, "type": event_type, "props": props_json},
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"log_event failed ({event_type}, {telegram_id}): {e}")


def log_event(telegram_id: int, event_type: str, **props) -> None:
    """Залогировать событие в фоне. Никогда не бросает в вызывающий поток."""
    task = asyncio.create_task(_insert_event(telegram_id, event_type, props or None))
    _background.add(task)
    task.add_done_callback(_background.discard)
```

Note: `gen_random_uuid()` is provided by the `pgcrypto`/`pg_catalog` extension; on Supabase it is available by default. If the migration environment lacks it, the model default (`uuid.uuid4`) is not used here because we INSERT via raw SQL — so we generate server-side. (Verified available on Supabase.)

- [ ] **Step 2: Add the import in handlers.py**

In `backend/app/bot/handlers.py`, add near the other `from app...` imports:
```python
from app.analytics import log_event
```

- [ ] **Step 3: Log `start`**

In `cmd_start` (`handlers.py:~79`), immediately after the `await get_or_create_user(...)` call, add:
```python
    log_event(user.id, "start")
```

- [ ] **Step 4: Log `solve`**

In `_solve_incoming` (`handlers.py:~359`), immediately after `await consume_quota(user_id, username=username)`, add:
```python
        log_event(user_id, "solve")
```
(Same indentation as `consume_quota` — inside the `try` block.)

- [ ] **Step 5: Log `paywall_shown` (both sites)**

(a) In `_solve_incoming` (`handlers.py:311`), the block is:
```python
    if not quota.allowed:
        kb = main_menu_keyboard(is_premium=quota.is_premium, is_admin=quota.is_admin)
        await message.answer(MSG_QUOTA_EXCEEDED, reply_markup=kb)
        return
```
Add `log_event(user_id, "paywall_shown")` right before `await message.answer(MSG_QUOTA_EXCEEDED, ...)`.

(b) In `handle_pick_task` (`handlers.py:~436`), find the second `await ...answer(MSG_QUOTA_EXCEEDED, ...)`. Add `log_event(<tg_id_in_scope>, "paywall_shown")` right before it, using the telegram id variable already in scope there (e.g. `callback.from_user.id`). Open the function to confirm the exact variable name before editing.

- [ ] **Step 6: Syntax-check**

Run: `python3 -m py_compile backend/app/analytics.py backend/app/bot/handlers.py`
Expected: no output (exit 0).

- [ ] **Step 7: Commit**

```bash
git add backend/app/analytics.py backend/app/bot/handlers.py
git commit -m "feat: log start/solve/paywall events"
```

---

## Task 4: Admin router (`/stats`, `/broadcast`) + registration

**Files:**
- Create: `backend/app/bot/admin.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Create the admin router**

`backend/app/bot/admin.py`:
```python
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
```

Note: HTML parse_mode is the bot-wide default (set in `main.py` Bot construction), so `message.answer` renders the `<b>`/`<code>` tags — no per-call `parse_mode` needed, consistent with the rest of the code.

- [ ] **Step 2: Register the admin router in main.py**

In `backend/app/main.py`, add the import alongside the other bot imports:
```python
from app.bot import admin
```
And register it **before** `handlers.router` (so admin commands win over the generic photo/text handlers), next to the `tg_stars.router` line (~35):
```python
dp.include_router(tg_stars.router)
dp.include_router(admin.router)
dp.include_router(handlers.router)
```

- [ ] **Step 3: Syntax-check**

Run: `python3 -m py_compile backend/app/bot/admin.py backend/app/main.py`
Expected: no output (exit 0).

- [ ] **Step 4: Commit**

```bash
git add backend/app/bot/admin.py backend/app/main.py
git commit -m "feat: admin /stats and /broadcast commands"
```

---

## Task 5: Integration verification + deploy

**Files:** none (verification only).

- [ ] **Step 1: Full syntax sweep**

Run: `python3 -m py_compile backend/app/models/event.py backend/app/notify.py backend/app/premium_notify.py backend/app/analytics.py backend/app/bot/handlers.py backend/app/bot/admin.py backend/app/main.py backend/app/models/__init__.py backend/alembic/versions/0003_events.py`
Expected: no output (exit 0).

- [ ] **Step 2: Build + start the stack (local Docker if available, else deploy to prod)**

Local: `docker compose up -d --build backend && docker compose logs backend --since 30s | tail -30`
Prod (git-based, per deploy-infra): push, then on server `cd /root/academ4i && git pull origin main && docker compose run --rm backend alembic upgrade head && docker compose up -d --build backend`.
Expected startup log: `DB pool initialized` → `Redis connected` → `Webhook set` → `Application startup complete`, no tracebacks.

- [ ] **Step 3: Smoke-test via the bot (as admin user `manag31`)**

  1. `/start` → welcome arrives.
  2. Solve a task (send a photo) → solution delivered.
  3. `/stats` → report renders with non-zero `start`/`solve`, HTML formatted, no error.
  4. `/broadcast тест рассылки` → preview + confirm buttons → tap «Отправить» → message arrives in the same chat, report `отправлено 1, не дошло 0`.
  5. `/broadcast тест` → tap «Отмена» → «Рассылка отменена».

- [ ] **Step 4: Verify rows landed**

Run a SQL check (via Supabase or `docker compose exec backend python -c`):
`SELECT type, COUNT(*) FROM events GROUP BY type;`
Expected: rows for `start` and `solve` (and `paywall_shown` if the free quota was exhausted during the smoke test).

- [ ] **Step 5: Final commit (if any verification fixups were needed)**

```bash
git add -A && git commit -m "fix: analytics/admin verification fixups"
```

---

## Self-Review

- **Spec coverage:** events table (Task 1) ✓; `log_event` fire-and-forget (Task 3) ✓; instrumentation at start/solve/paywall×2 (Task 3) ✓; `notify.py` shared sender + premium refactor (Task 2) ✓; admin router registered before handlers (Task 4) ✓; `/stats` audience+funnel+revenue (Task 4) ✓; `/broadcast` two-step confirm to all (Task 4) ✓; error handling — `log_event` swallows, broadcast per-recipient swallow in `send_one` (Tasks 2,3) ✓; migration via Session pooler note (Task 1) ✓; verification (Task 5) ✓.
- **Placeholder scan:** the only "open the function to confirm variable name" is in Task 3 Step 5(b) — unavoidable since the exact var in `handle_pick_task` wasn't read; instruction is explicit and bounded. No TBD/TODO elsewhere.
- **Type consistency:** `send_one(bot, tg, text, reply_markup)` and `broadcast_send(bot, telegram_ids, text, reply_markup) -> (sent, failed)` and `SEND_DELAY_SEC` used identically in Tasks 2 and 4; `log_event(telegram_id, event_type, **props)` used consistently in Task 3; `_STATS_SQL` field names match the `report` f-string keys.
