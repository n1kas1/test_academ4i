# Аналитика событий + админ-команды (`/stats`, `/broadcast`)

Дата: 2026-05-23
Статус: дизайн утверждён (автономно), к реализации.

## Цель

Дать владельцу бота видимость и инструмент влияния:
- **`/stats`** — аудитория/активность и воронка конверсии (текстом, в боте).
- **`/broadcast`** — простая рассылка сообщения всем юзерам.
- **Лог событий** — фундамент под текущие метрики и будущий бэклог (рефералка, 👍/👎, UTM).

Не входит в скоуп: веб-дашборд, внешние сервисы аналитики, сегментированная рассылка, A/B, метрики качества/стоимости (cache-hit и т.п.), pytest-харнесс.

## Подход

Свой append-only лог событий в Postgres (вариант A). Команды — в самом боте, под `is_admin`. Деньги (`purchase`) НЕ дублируем в события — авторитетный источник остаётся `payments`.

## 1. Данные: таблица `events`

Новая модель `Event` (`backend/app/models/event.py`), по образцу `solution.py`:

| поле | тип | примечание |
|---|---|---|
| `id` | UUID PK (`default=uuid.uuid4`) | |
| `telegram_id` | BigInteger, `index=True` | без FK — лог переживает удаление юзера |
| `type` | String(32), `index=True` | `start` \| `solve` \| `paywall_shown` |
| `props` | JSONB, nullable | контекст события |
| `created_at` | `DateTime(timezone=True)`, `server_default=func.now()`, `index=True` | НЕ через `TimestampMixin` (не нужен `updated_at`) |

Композитный индекс `ix_events_type_created (type, created_at)` — под агрегаты `/stats`.

`props` по типам:
- `solve`: `{"topic": "matan", "cache_hit": true}`
- `start`, `paywall_shown`: `{}` (или пусто)

Регистрация в `backend/app/models/__init__.py` (`Event` в импорты и `__all__`).

Миграция: `backend/alembic/versions/0003_events.py` (следующий номер после `0002_user_credits.py`). Autogenerate, затем проверить руками. На Supabase при ошибке prepared statements — накат через Session pooler (порт 5432), см. DEPLOY.md шаг 4.

## 2. Слой логирования: `backend/app/analytics.py`

```python
async def log_event(telegram_id: int, type: str, **props) -> None
```

Жёсткие требования:
- **Никогда не роняет пользовательский поток** и **не добавляет задержку в hot-path решения**.
- Реализация: fire-and-forget через `asyncio.create_task`, внутри — своя `get_session()`, весь INSERT в `try/except`, ошибка → `logger.warning`.
- Вспомогательная корутина `_insert_event(...)` выполняет реальный INSERT; публичная `log_event` лишь создаёт задачу.

Точки вызова (строки на момент дизайна):
- **`start`** — `handlers.py` `cmd_start`, после `get_or_create_user` (~стр. 87).
- **`solve`** — `handlers.py` `_solve_incoming`, после успешной отправки решения и `consume_quota` (~стр. 359). props: `topic` и `cache_hit` из `result` (поля уточнить при реализации по объекту result из `solve_task_from_photo`).
- **`paywall_shown`** — оба места отправки `MSG_QUOTA_EXCEEDED`: `handlers.py:311` (`_solve_incoming`) и `handlers.py:436` (`handle_pick_task`).

## 3. Переиспользуемый рассыльщик: `backend/app/notify.py`

Выносим троттлинг-примитив из `premium_notify.py`:

```python
async def broadcast_send(
    bot: Bot,
    telegram_ids: Iterable[int],
    text: str,
    reply_markup=None,
) -> tuple[int, int]:  # (sent, failed)
```

- Логика `_send_once` (обработка `TelegramRetryAfter` с одним повтором) + пейсинг `_SEND_DELAY_SEC` (~20 msg/s) переезжают сюда.
- `premium_notify._notify` рефакторится: дедуп (`redis.exists`/`set`) остаётся в `premium_notify`, но per-send отправка идёт через общий примитив `notify`. Семантика «помечаем дедуп только при успехе» сохраняется (broadcast_send возвращает успех/неуспех — но для дедупа premium_notify нужен per-recipient результат; см. ниже).

Уточнение по premium_notify: чтобы сохранить per-recipient дедуп, общий примитив экспортирует и низкоуровневую `send_one(bot, tg, text, reply_markup) -> bool` (RetryAfter+повтор), а `broadcast_send` = цикл `send_one` + пейсинг + счётчики. `premium_notify` использует `send_one` напрямую (как сейчас `_send_once`), а `/broadcast` использует `broadcast_send`. Так дублирование уходит, а дедуп-семантика premium не ломается.

## 4. Админ-роутер: `backend/app/bot/admin.py` (новый)

`router = Router()`, регистрируется в `main.py` **до** `handlers.router` (рядом с `tg_stars.router`, стр. 35). Все хендлеры начинают с проверки `is_admin(message.from_user.username)`; не-админ → молча игнор или короткий ответ.

### `/stats`
Агрегирующие SQL (raw `text()` допустим — есть прецедент в проекте). Период по умолчанию — сводка за всё время + срезы 1/7/30 дней. Отчёт текстом (HTML), секции:

**Аудитория/активность**
- Всего юзеров (`COUNT(*) FROM users`).
- Новые за 1/7/30 дней (`users.created_at`).
- DAU / WAU = `COUNT(DISTINCT telegram_id) FROM events WHERE created_at > now() - interval '1 day' / '7 days'`.
- Решено задач за 1/7/30 дней (`COUNT(*) FROM events WHERE type='solve'`).

**Воронка конверсии** (распределённые когорты, каждый шаг — кол-во distinct юзеров):
1. `start` — distinct telegram_id с событием `start` (≈ всего юзеров).
2. `solve ≥ 1` — distinct telegram_id с событием `solve`.
3. `paywall_shown` — distinct telegram_id с событием `paywall_shown`.
4. `purchase` — `COUNT(DISTINCT telegram_id) FROM payments WHERE status='succeeded'`.

Для каждого шага — абсолют и % от предыдущего (где отвал).

**Деньги (бонусом, дёшево)**
- Выручка ⭐ за 7/30 дней (`SUM(amount_stars) FROM payments WHERE status='succeeded'`).
- Покупки premium vs pack (`GROUP BY product`).

### `/broadcast`
Двухшаговый, защита от случайного блас­та:
1. `/broadcast <текст>` (HTML). Если текста нет — подсказка по формату.
2. Бот сохраняет текст (в Redis по admin telegram_id, TTL ~10 мин) и показывает превью + «отправить N юзерам?» с inline-кнопками `broadcast:confirm` / `broadcast:cancel` (N = `COUNT(*) FROM users`).
3. На `confirm` — выбрать все `telegram_id` из `users`, вызвать `broadcast_send`, ответить отчётом `отправлено X / не дошло Y`. На `cancel` — отбой, очистить Redis.

Callback-хендлеры `broadcast:*` — в этом же роутере, тоже под `is_admin`.

## 5. Обработка ошибок

- `log_event`: фоновая задача; любые исключения → `logger.warning`, на юзера не влияют.
- `/broadcast`: per-recipient ошибки (бот заблокирован и т.п.) глотаются внутри `send_one`/`broadcast_send`, считаются в `failed`. Обязательное подтверждение до старта. RetryAfter обрабатывается примитивом.
- `/stats`: при пустой БД запросы возвращают 0 — отчёт корректно показывает нули.

## 6. Изменяемые / новые файлы

Новые:
- `backend/app/models/event.py`
- `backend/alembic/versions/0003_events.py`
- `backend/app/analytics.py`
- `backend/app/notify.py`
- `backend/app/bot/admin.py`

Изменяемые:
- `backend/app/models/__init__.py` — регистрация `Event`.
- `backend/app/bot/handlers.py` — 4 вызова `log_event` (start, solve, paywall ×2).
- `backend/app/premium_notify.py` — переезд на общий `send_one` из `notify.py`.
- `backend/app/main.py` — `dp.include_router(admin.router)` до `handlers.router`.

## 7. Проверка («чтобы всё работало»)

- `python -m py_compile` по всем новым/изменённым файлам.
- `alembic upgrade head` накатывает `0003_events` без ошибок (локальный Docker или прод через Session pooler).
- Импорт-граф поднимается (контейнер стартует, lifespan без ошибок).
- Smoke через бот: `/start` → есть строка в `events`; решить задачу → `solve`; упереться в лимит → `paywall_shown`; `/stats` отдаёт корректные числа; `/broadcast тест` → подтверждение → сообщение приходит, отчёт корректен.
- Идемпотентность/деньги не затрагиваются — payments не меняем.

## 8. Зависимости между задачами (для делегирования)

1. Модель `Event` + миграция `0003` + регистрация в `__init__`.
2. `notify.py` (рефактор примитива) + переезд `premium_notify`.
3. `analytics.py` + инструментирование `handlers.py` — зависит от (1).
4. `admin.py` (`/stats` + `/broadcast`) + регистрация в `main.py` — `/stats` зависит от (1), `/broadcast` зависит от (2).
