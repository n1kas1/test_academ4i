# Security review — Academ4I bot (2026-06-08)

Полный аудит всего проекта: 8 доменных аудиторов + adversarial-верификация каждой
находки (65 агентов). Подтверждена **31 находка**. Секреты в отчёте отредактированы.

Сводка по severity: **2 CRITICAL** (один корень), **8 HIGH**, **4 MEDIUM**, **17 LOW**.

---

## CRITICAL — сделать в первую очередь

### C1. Боевой `backend/.env` запекается в Docker-образ
- **Где:** `backend/.env` (живые секреты) + `backend/Dockerfile:28` (`COPY . .`) + **нет `.dockerignore`**; build context = `./backend`.
- **Факт:** `.env` НЕ в git (gitignore ✓, история чистая), но `COPY . .` без `.dockerignore` копирует его в слой образа `/app/.env`. В образе сейчас: ProxyAPI-ключ (он же ANTHROPIC и OPENAI), пароль БД Supabase, Telegram bot token.
- **Эксплуатация:** любой с доступом к образу (`docker save`/`docker history`, snapshot диска VPS, доступ к Docker socket соседнего контейнера) достаёт все секреты. Усиливается находкой H1 (LLM может прочитать `/app/.env` через LaTeX).
- **Фикс:**
  1. Создать `backend/.dockerignore`: `.env`, `.env.*`, `.venv`, `__pycache__`, `*.pyc`, `render_cache`, `tests`, `.git`, `.pytest_cache`.
  2. Пересобрать образ, убедиться что `/app/.env` отсутствует (`docker run --rm academ4i-backend ls -la /app/.env` → нет).
  3. **Ротировать секреты** как потенциально скомпрометированные, если образ куда-либо уходил (registry/бэкап/шаринг): bot token (@BotFather /revoke), пароль роли Supabase, ключ ProxyAPI. Если образ всегда был только на этом VPS и доступа к нему ни у кого не было — ротация на усмотрение, но `.dockerignore` обязателен.

---

## HIGH

### H1. LLM-LaTeX может читать произвольные файлы сервера (`\input`/`\openin`/`\includegraphics`) → LFI + эксфильтрация через PNG
- **Где:** `render/latex_to_png.py` (`_compile_sync`, LATEX_TEMPLATE), `render/figures.py` (STANDALONE_TEMPLATE, `_sanitize_figure_body`).
- **Факт:** `-no-shell-escape` отключает только `\write18`, но НЕ `\input`/`\openin`/`\read`; `openin_any` в окружении не ограничен; `\usepackage{graphicx}` подключён. Тело от LLM подставляется в шаблон без фильтрации файловых примитивов.
- **Эксплуатация:** prompt-injection в условии (free-mode) заставляет солвер вставить `\ans{\input{/app/.env}}` или TikZ с `\input` → содержимое файла попадает в PDF/PNG, который бот присылает юзеру. Прямое чтение `.env`/исходников.
- **Фикс (на границе рендера, не на поведении LLM):** запускать оба pdflatex (основной + figures) с `env={..., "openin_any": "p", "openout_any": "p", "TEXMFOUTPUT": tmp}` (paranoid — чтение/запись только в текущем каталоге) + добавить в санитайзер удаление `\input`, `\include`, `\openin`, `\read`, `\write`, `\InputIfFileExists`, `\immediate`, `\catcode`, `\csname` из LLM-вывода (и тела рисунков). Это закрывает и C1-усиление.

### H2. Дефолт `telegram_webhook_secret = 'change-me'` — fail-open
- **Где:** `config.py:18`; используется в `main.py:84,153`.
- **Факт:** у секрета есть дефолт (в отличие от bot_token/api_key, у которых нет → ValidationError). Бот молча стартует с предсказуемым `change-me`, если оператор не задал секрет.
- **Эксплуатация:** атакующий, зная публичный дефолт, шлёт поддельные апдейты на `/webhook` → инъекция фейковых сообщений/команд (в т.ч. от «админа»).
- **Фикс:** убрать дефолт (`telegram_webhook_secret: str` без значения) → fail-loud; или `field_validator`, отклоняющий `change-me`/короткие. Бонус: сравнение через `hmac.compare_digest` вместо `!=` (constant-time, H3).

### H3. Сравнение webhook-секрета не constant-time
- **Где:** `main.py:153` (`!=`). Timing-side-channel (низкая практическая эксплуатируемость), но чинится вместе с H2 через `hmac.compare_digest`.

### H4. Нет защиты от decompression bomb при декодировании изображений
- **Где:** `ai/vision.py:26` (`Image.open` на байтах юзера), нет `Image.MAX_IMAGE_PIXELS`/`verify()`.
- **Эксплуатация:** PNG/TIFF с заявленным разрешением ~50000×50000 (физически несколько МБ) → PIL разворачивает в гигабайты RAM → OOM/краш воркера.
- **Фикс:** `Image.MAX_IMAGE_PIXELS = 40_000_000` в начале vision.py + проверка `img.size` до `convert/resize` + ловить `DecompressionBombError`. Также применить в `_pdf_first_page_png` (poppler рендерит произвольный PDF).

### H5. Bind-mount всего исходника в контейнер на проде (`./backend:/app`, RW)
- **Где:** `docker-compose.yml:22`.
- **Риск:** любая RCE в контейнере → запись в исходники на хосте (persistence). Также код образа игнорируется (едет с диска).
- **Фикс:** убрать bind-mount из прод-compose (образ уже содержит код через `COPY . .`); hot-reload вынести в `docker-compose.override.yml` для dev.

### H6. `.venv` копируется в образ (тот же `COPY . .` без `.dockerignore`)
- Закрывается тем же `.dockerignore`, что и C1. Раздувает образ + потенциально чужие бинарники.

### (H1 и «file-read через TikZ» — две находки из разных доменов про одно; фикс H1 закрывает обе.)

---

## MEDIUM

### M1. Авторизация админа по username, а не по `telegram_id` (спуфинг)
- **Где:** `ratelimit.py:120-124` (`is_admin`), `config.py:admin_usernames`.
- **Риск:** username в Telegram меняется; если админ освободит username, его может занять другой и получить полный доступ к `/stats`, `/broadcast`.
- **Фикс:** проверять по числовому `telegram_id` (whitelist ID в конфиге), не по username.

### M2. Безлимитный параллельный fan-out компиляции `%%FIG`
- **Где:** `render/figures.py:234-242` (`asyncio.gather` по ВСЕМ блокам без cap).
- **Риск:** решение с 30+ `%%FIG` → 30 параллельных pdflatex → CPU/PID/RAM-исчерпание одним запросом.
- **Фикс:** cap числа блоков (6-8, остальные молча убрать) + общий `asyncio.Semaphore` на компиляцию рисунков.

### M3. Контейнер работает от root (нет `USER` в Dockerfile)
- **Фикс:** `RUN useradd -m -u 10001 appuser` + права на `/app/render_cache` + `USER appuser` перед CMD.

### M4. Дефолтный webhook-секрет допускается (дубль H2 из домена инфры) — закрывается фиксом H2.

---

## LOW (17) — defense-in-depth, по возможности

- **L1** `<TASK>`/`<HINT>` изоляция чувствительна к регистру (`_strip_isolation_tags`) → обход строчным `</task>`. Фикс: регистронезависимый regex.
- **L2** На фото-ветке НЕТ topic-gate (есть только на тексте) → индиректная инъекция через текст на фото. Фикс: вызвать `is_math_or_physics(condition_text)` в `solve_task_from_photo` после OCR.
- **L3** Запрет раскрытия модели/провайдера держится только на промпте, нет output-фильтра. Фикс: `redact_identity_leaks(latex)` после sanitize.
- **L4** ProxyAPI-ключ Gemini идёт в URL query (`?key=`) → риск утечки в чужие логи. Фикс: заголовок `x-goog-api-key`.
- **L5** Логирование фрагментов условий/решений на INFO (`pipeline.py:477,603`, `claude.py:146`, `events.props latex_head`). Фикс: логировать длину/хэш, не контент (PII).
- **L6** `add_credits/_apply_credits` — read-modify-write без блокировки (гонка при двойной покупке). Фикс: атомарный `UPDATE ... credits = credits + :amt RETURNING`.
- **L7** Платёж: при падении между dedup-ключом и начислением кредиты теряются без идемпотентного восстановления. Фикс: снимать `upd:{id}` при исключении / транзакционная согласованность.
- **L8** Нет верхнего лимита длины текстового ввода перед каскадом LLM. Фикс: `condition_text[:MAX]`.
- **L9** Неограниченный рост `render_cache`/`figures` (нет TTL/эвикции). Фикс: периодическая очистка по `mtime` / LRU.
- **L10** Нет общего process-level семафора на subprocess рендера. Фикс: модульный `asyncio.Semaphore`.
- **L11** `pdf2image` рендерит произвольный PDF юзера (poppler). Фикс: лимит мегапикселей (вместе с H4).
- **L12** embeddings-клиент без таймаута (дефолт SDK 600с). Фикс: `timeout=15-30, max_retries=1`.
- **L13** Нет ресурсных лимитов контейнеров (CPU/RAM/pids). Фикс: `mem_limit/cpus/pids_limit` в compose.
- **L14** Redis без пароля/maxmemory. Фикс: `--requirepass` + `--maxmemory-policy noeviction` (осторожно: дедуп-ключи нельзя вытеснять).
- **L15** Незакреплённые версии `anthropic/openai/httpx` (range). Фикс: lock-файл с хэшами (pip-tools/uv).
- **L16** `_ensure_figure` тратит до 3× LLM-вызовов + 3× компиляций на рисунок; триггерится подстроками («график/схема/вектор») даже в нерелевантных задачах; зовётся и на cache-hit. Фикс: не звать на cache-hit; ужесточить триггер; кэшировать сгенерированный рисунок в решение.
- **L17** Запись в кэш не атомарна (гонка двух одинаковых `%%FIG`). Фикс: write в temp + `os.rename` (атомарно в пределах ФС).

---

## ReDoS (из домена инъекций)
Найдены regex с перекрывающейся альтернацией под квантификатором в `latex_sanitize.py`, выполняемые синхронно на event-loop. Подтверждено как реальный риск (низкий-средний). Стоит проверить на catastrophic backtracking и при необходимости упростить/ограничить длину входа.

---

## Что НЕ найдено (проверено и чисто)
- SQL-инъекции: все запросы параметризованы (включая pgvector/LIKE); `topic` из закрытого набора.
- Command injection: все `subprocess` — list-форма, `shell=False`, `\write18` отключён везде.
- Path traversal в именах файлов: только sha256-хэши + `tempfile`.
- SSRF: исходящие URL только из фиксированных `*_base_url`; user-данные в URL не попадают; TLS нигде не отключён.
- Платёж: идемпотентность по `charge_id` (UNIQUE) есть; `consume_credits` атомарен.

---

## Приоритет внедрения
1. **Сейчас:** `.dockerignore` (C1) + env `openin_any=p`/`openout_any=p` + фильтр `\input` и пр. в санитайзере (H1) + webhook secret fail-loud + compare_digest (H2/H3) + `Image.MAX_IMAGE_PIXELS` (H4).
2. **Скоро:** убрать прод bind-mount (H5), `USER` в Dockerfile (M3), админ по `telegram_id` (M1), cap fan-out рисунков (M2), ресурсные лимиты (L13).
3. **Ротация секретов** — если есть сомнение в изоляции образа.
