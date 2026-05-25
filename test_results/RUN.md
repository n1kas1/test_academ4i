# Как запустить eval_models.py

Скрипт переиспользует боевые модули academ4i (`app.config`, `app.ai.*`), поэтому
запускать его нужно в окружении, где есть:

1. **Код academ4i** (он здесь — `backend/`).
2. **Зависимости** из `backend/requirements.txt` (как минимум `anthropic`, `openai`,
   `sqlalchemy`, `asyncpg`, `pgvector`, `pydantic-settings`, `loguru`).
3. **`.env`** в `backend/.env` (рядом с кодом — `Settings(env_file=".env")` читает из CWD,
   запускаем из `backend/`). Минимум:
   - `ANTHROPIC_API_KEY` = твой ключ ProxyAPI (для Sonnet через нативный Anthropic SDK)
   - `OPENAI_API_KEY` = тот же ключ ProxyAPI (для Gemini/DeepSeek через OpenAI-совместимый шлюз
     и для эмбеддингов) — у ProxyAPI один токен на все провайдеры
   - `DATABASE_URL` = Supabase Postgres + pgvector (для идентичного RAG)
   - `TELEGRAM_BOT_TOKEN` = любая непустая заглушка (в тесте не используется, но поле
     обязательно для `Settings`)
   - base_url'ы можно не трогать — дефолты в `config.py` уже верные.

> ⚠️ Запущенные локально контейнеры `tgram-bot-*` — это ДРУГОЙ проект, не academ4i.
> Для теста они не годятся.

## Перед запуском — проверь КОНФIG в `scripts/eval_models.py`

- `MODELS` — точные model-id для DeepSeek и Gemini (сейчас плейсхолдеры).
- `RATES_RUB_PER_MTOK` — ставки ProxyAPI в ₽/M (Sonnet — факт, остальные — плейсхолдеры).

## Вариант A — локальный venv (рекомендуется)

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp /путь/к/academ4i/.env ./.env        # или положить .env с нужными ключами
cd ..
python backend/.venv/bin/python scripts/eval_models.py
# либо: (cd backend && PYTHONPATH=. .venv/bin/python ../scripts/eval_models.py)
```

Скрипт ходит в Supabase pgvector по сети — VPS не нужен.

## Вариант B — собрать docker-образ academ4i локально

```bash
# положить .env в корень репо, затем:
docker compose build backend
docker compose run --rm -v "$PWD/scripts:/app/scripts" \
    -v "$PWD/test_results:/app/test_results" \
    backend python scripts/eval_models.py
```

## Результат

- `test_results/results/NN_<model>.md` — ответ модели + cost + time + шаблон оценки
- `test_results/results/NN_rag.md` — RAG-контекст (един для всех моделей)
- `test_results/results.json` — машиночитаемые метрики
- `test_results/summary.md` — сводные таблицы (cost / time / проекция) + шаблон оценки качества
