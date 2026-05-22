# Деплой academ4i на Kamatera VPS

Стек: FastAPI + aiogram3 + Redis + Supabase Postgres. Caddy у тебя уже стоит от academvoice — добавим в его конфиг один блок.

---

## 1. Зайти на VPS

```bash
ssh root@185.139.230.135
# или твой обычный пользователь, как ходишь в academvoice
```

## 2. Залить код academ4i на VPS

Самый простой путь — через git. Если ты ещё не закоммитил локально:

**На своём Mac:**
```bash
cd /Users/nikas/Downloads/neonlust/academ4i
git init
git add .
git commit -m "academ4i initial"
# Создай приватный репо на github (или используй existing академvoice org), потом:
git remote add origin git@github.com:n1kas/academ4i.git
git push -u origin main
```

**На VPS:**
```bash
cd ~                       # или туда где у тебя academvoice
git clone git@github.com:n1kas/academ4i.git
cd academ4i
```

**Альтернатива без git** — через `scp` прямо с Mac (но git удобнее для будущих обновлений):
```bash
scp -r /Users/nikas/Downloads/neonlust/academ4i root@185.139.230.135:~/academ4i
```

## 3. Положить .env на VPS

`.env` я уже сгенерировал у тебя локально (`/Users/nikas/Downloads/neonlust/academ4i/.env`). **В git его НЕ коммить**.

Загрузи через `scp`:
```bash
scp /Users/nikas/Downloads/neonlust/academ4i/.env root@185.139.230.135:~/academ4i/.env
```

## 4. Накатить миграции на Supabase (один раз)

```bash
cd ~/academ4i
docker compose run --rm backend alembic upgrade head
```

Это создаст таблицы users / solutions / payments + HNSW индекс по embedding.

**Если вылетит ошибка про prepared statements** — это Transaction pooler Supabase. Подключаемся напрямую к Session pooler для миграций:

```bash
# Временно подменим DATABASE_URL для миграций (порт 5432 вместо 6543).
# Подставь реальные значения из своего .env (PROJECT_REF / PWD), сюда секреты НЕ коммитим.
DATABASE_URL='postgresql+asyncpg://postgres.PROJECT_REF:PWD@aws-1-eu-central-1.pooler.supabase.com:5432/postgres' \
  docker compose run --rm -e DATABASE_URL backend alembic upgrade head
```

## 5. Поднять стек

```bash
docker compose up -d --build
docker compose logs -f backend
```

Бэкенд должен стартовать, зарегистрировать webhook в TG и слушать `127.0.0.1:8001`. Должно появиться:
```
INFO  Starting academ4i...
INFO  DB pool initialized
INFO  Redis connected
INFO  Webhook set: https://academ4i.duckdns.org/webhook
```

## 6. Добавить academ4i в Caddy от academvoice

Найди где у тебя лежит `Caddyfile` для academvoice. Скорее всего что-то вроде:
```bash
ls -la ~/academvoice/Caddyfile        # или /etc/caddy/Caddyfile
```

Открой его и добавь **в конец** новый блок:

```caddy
academ4i.duckdns.org {
    reverse_proxy 127.0.0.1:8001

    header {
        Strict-Transport-Security "max-age=31536000;"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        -Server
    }

    log {
        output file /var/log/caddy/academ4i.log
        format json
    }
}
```

Перезагрузи Caddy (зависит от того как он у тебя запущен):

```bash
# Если Caddy в docker академvoice:
cd ~/academvoice && docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile

# Если Caddy systemd:
sudo systemctl reload caddy
```

Caddy сам автоматически выпустит Let's Encrypt сертификат для `academ4i.duckdns.org` за 30-60 сек.

## 7. Проверить

```bash
# Health-check бэкенда напрямую
curl http://localhost:8001/health
# → {"status":"ok"}

# Через домен (HTTPS)
curl https://academ4i.duckdns.org/health
# → {"status":"ok"}

# Проверить вебхук Telegram
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
# должно вернуть url=https://academ4i.duckdns.org/webhook
```

## 8. Smoke-test самого бота

Зайди в Telegram, открой `@Academ4I_bot`, нажми `/start`.

Должен ответить welcome-сообщением. Потом сфоткай задачу из своего конспекта матана и кинь в бот — должно прийти решение за 10-25 сек.

---

## Если упадёт

```bash
docker compose logs -f backend | tail -100   # смотрим ошибки
docker compose restart backend                # перезапуск без пересборки
docker compose down && docker compose up -d --build  # полная пересборка
```

Типичные косяки:
- **403 при вызове proxyapi.ru** → проверь баланс в proxyapi.ru
- **`relation "users" does not exist`** → миграции не накатились, см. шаг 4
- **Webhook не работает** → проверь `getWebhookInfo`, должен быть https + правильный домен
- **Vector type not found** → search_path не подтянулся, в `app/core/db.py` уже включено `public,extensions`

---

## Что дальше после деплоя

1. Парсинг учебников (один раз): `docker compose exec backend python scripts/parse_textbook.py textbooks/Demidovich.pdf --source "Демидович" --topic matan`
2. RAG-обогащение pipeline (после того как кэш есть)
3. TG Stars подписки (когда первый платящий захочет)
