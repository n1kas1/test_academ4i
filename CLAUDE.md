# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

Telegram-бот (`@Academ4I_bot`) — AI-решатель задач по матану, линейной алгебре и алгебре (теория групп) для студентов техвузов РФ. Юзер шлёт фото задачи → бот возвращает пошаговое решение в виде PNG (отрендеренный LaTeX) + сырой LaTeX для копирования.

## Команды

Всё запускается в Docker (локальной dev-среды без контейнера нет — Python-зависимости включают TeX Live и poppler).

```bash
# Запуск стека (backend + redis)
docker compose up -d --build
docker compose logs -f backend

# Миграции (один раз / после изменения моделей)
docker compose run --rm backend alembic upgrade head

# Новая миграция
docker compose run --rm backend alembic revision --autogenerate -m "описание"

# Парсинг учебника в RAG-базу (PDF → чанки → pgvector)
docker compose exec backend python scripts/parse_textbook.py \
    textbooks/Demidovich.pdf --source "Демидович" --topic matan
# --estimate: оценка $/страниц без вызова API; --start N / --end M: диапазон страниц

# Health-check
curl http://localhost:8001/health
```

Тестов в репозитории нет. Smoke-test — через сам Telegram-бот.

**Важно про миграции на Supabase:** при ошибке prepared statements (Transaction pooler, порт 6543) накатывай через Session pooler (порт 5432), временно подменив `DATABASE_URL`. См. [DEPLOY.md](DEPLOY.md) шаг 4.

## Архитектура

**Точка входа** — [backend/app/main.py](backend/app/main.py): FastAPI с aiogram-вебхуком на `POST /webhook` (секрет через заголовок `X-Telegram-Bot-Api-Secret-Token`). При старте lifespan инициализирует БД и Redis, регистрирует вебхук в Telegram и запускает фоновую задачу `premium_notifier_loop` (см. ниже), отменяя её при shutdown. Порядок роутеров критичен: `tg_stars.router` и `admin.router` идут **до** `handlers.router` — чтобы `pre_checkout_query`/`successful_payment` и админ-команды ловились раньше «всеядного» обработчика фото/текста.

**AI-pipeline** — [backend/app/ai/pipeline.py](backend/app/ai/pipeline.py), сердце проекта. `solve_task_from_photo()`:
1. `prepare_image` (vision.py) — base64 + resize
2. `extract_condition_text` — лёгкий Claude vision OCR → текст условия (с `user_hint` для выбора нужной задачи, если на фото несколько)
3. `classify_topic` — эвристика по ключевым словам (matan/lin_alg/groups/rings_fields/polynomials)
4. `embed_text` — OpenAI embedding (1536d)
5. `find_similar_solutions` — поиск в pgvector
6. **Cache hit** при `cosine_sim > CACHE_HIT_THRESHOLD` (0.87) **только среди `source='generated'`** → отдаём готовое решение ($0). Учебники без решений как готовый ответ не отдаём.
7. **Cache miss** → `build_rag_context` (топ-3 похожих) → `solve_with_claude_vision` → `save_solution`
8. `render_latex_to_png`

**Роутинг модели:** `is_complex_task()` решает, включать ли extended thinking. Доказательства/исследования («докажите», «найдите все», «при каких») → с thinking (дороже, точнее); вычисления → без.

**Валидация кэша:** `_is_valid_latex()` отклоняет cached-решения в устаревшем HTML-формате (с эмодзи/тегами) — они перегенерируются в текущий LaTeX-формат. При правке формата вывода учитывай эти маркеры.

**Слои `app/`:**
- `ai/` — vision, claude, embeddings, retrieval, pipeline (Claude и OpenAI ходят через **ProxyAPI.ru**, см. base_url в config)
- `bot/` — handlers, keyboards, messages; `admin.py` — админ-команды `/admin` (меню), `/stats` (метрики за период с переключателем), `/broadcast` (рассылка с подтверждением, черновик в Redis)
- `payments/tg_stars.py` — Telegram Stars (подписка + разовый пакет)
- `core/` — db (asyncpg/SQLAlchemy async), redis
- `render/latex_to_png.py` — LaTeX → PDF → PNG через TeX Live (кэш по hash содержимого)
- `models/` — user, solution, payment, event (+ `base.py` — declarative base, `TimestampMixin`); `ratelimit.py` — квоты и rate limit через Redis
- `notify.py` — общий троттлящий рассыльщик (`send_one`/`broadcast_send`, ~20 msg/s, обработка flood-control 429). Используют и `premium_notify`, и `/broadcast`
- `analytics.py` — fire-and-forget лог продуктовых событий (`log_event` → таблица `events`, типы: start/solve/paywall_shown). Никогда не бросает в hot-path
- `premium_notify.py` — фоновый цикл (`premium_notifier_loop`, проверка раз в 3 ч): шлёт напоминания о Premium «скоро закончится» (≤2 дней) и «закончился» (последние 3 дня). Дедупликация — через Redis-ключи с TTL ~40 дней. Запускается из lifespan, не cron/отдельный процесс

**БД:** Supabase Postgres + pgvector. Главная таблица `solutions` (task_text, embedding vector(1536), topic, source, solution_markdown, usage_count) — служит и кэшем сгенерированных решений, и RAG-базой учебников. `source` различает учебник («Демидович (стр. 42)») и `generated`. `search_path` должен включать `public,extensions` (иначе «vector type not found»).

## Тарифы (в config.py)

Free: **2 задачи на скользящее окно 7 дней** (`free_tasks_per_week` / `free_window_days`) — потолок жёсткий, без накопления (антифарм): окно открывается при первой бесплатной задаче и сбрасывается только спустя 7 дней. Дальше — подписка (149⭐/30 дней безлимит) или разовый пакет (79⭐/5 задач, без срока). Списание квоты атомарно одним `UPDATE` с CASE-выражениями (порядок: premium → credits → free), см. `consume_quota` в `ratelimit.py`. Админы (`admin_usernames`) — безлимит.

## Конфиг

Всё через `.env` (pydantic-settings), шаблон в [.env.example](.env.example). Модель Claude и флаги (`claude_use_extended_thinking`), цены, лимиты, админы — в [backend/app/config.py](backend/app/config.py).

## Деплой

Kamatera VPS + Docker. Backend биндится на `127.0.0.1:8001`; HTTPS-проксирование делает **внешний Caddy от соседнего проекта academvoice** (в этом compose Caddy НЕ запускается). Подробности — [DEPLOY.md](DEPLOY.md).
