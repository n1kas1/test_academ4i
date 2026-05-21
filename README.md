# Academ4I

Telegram бот: AI-решатель задач по матану, линейной алгебре, алгебре (теория групп) и др. для студентов технических вузов РФ.

**Бот:** [@Academ4I_bot](https://t.me/Academ4I_bot)

## Стек

- **Backend:** FastAPI (async) + aiogram 3
- **БД:** Supabase Postgres + pgvector (RAG по учебникам)
- **Кэш:** Redis
- **AI vision (OCR формул):** Mathpix
- **AI reasoning:** Claude 3.7 Sonnet (extended thinking) через ProxyAPI.ru
- **Эмбеддинги:** OpenAI text-embedding-3-small
- **Платежи:** Telegram Stars Subscriptions (299₽/мес безлимит)
- **Хостинг:** Kamatera VPS + Docker + Caddy (HTTPS)

## Архитектура AI-pipeline

```
[Фото в TG] → aiogram handler
            ↓
        rate limit (Redis)
            ↓
        квота юзера (Free/Premium)
            ↓
        Mathpix OCR → LaTeX
            ↓
        классификация темы (matan/lin_alg/groups/...)
            ↓
        эмбеддинг → поиск в pgvector (топ-5 похожих задач+теории)
            ↓
        cosine_sim > 0.93 → готовое решение из кэша (1 сек, $0)
            ↓ (иначе)
        Claude 3.7 + RAG-контекст → пошаговое решение
            ↓
        сохранение в кэш (для будущих юзеров)
            ↓
        отправка юзеру (markdown + LaTeX)
```

## Тарифы

- **Free:** 7-дневный триал безлимит, потом 0 задач (до подписки)
- **Premium:** 299₽/мес безлимит через TG Stars Subscriptions

## База учебников (RAG)

- Демидович — главный задачник матан
- Виноградова-Олехник-Садовничий т.1-3 — задачник матан МГУ
- Антидемидович т.1-6 — полные решения к Демидовичу (few-shot примеры)
- Кострикин-2009 — главный задачник по алгебре (включая теорию групп)
- алгебра.pdf — учебник теории

## Quick Start

```bash
cp .env.example .env  # заполнить ключи
docker-compose up --build
```

После запуска: webhook на `https://<домен>/webhook`, нужно зарегистрировать в @BotFather.

## Структура

```
academ4i/
├── backend/                 # FastAPI + aiogram
│   ├── app/
│   │   ├── main.py          # entry point
│   │   ├── config.py        # настройки
│   │   ├── core/            # db, redis
│   │   ├── models/          # ORM-модели
│   │   ├── bot/             # handlers, keyboards, messages
│   │   ├── ai/              # mathpix, claude, embeddings, retrieval
│   │   └── payments/        # TG Stars subscriptions
│   ├── alembic/             # миграции
│   └── requirements.txt
├── scripts/
│   └── parse_textbook.py    # парсинг PDF → чанки → pgvector
├── textbooks/               # PDF учебников (gitignore-ed)
├── docker-compose.yml
├── Caddyfile
└── .env.example
```
