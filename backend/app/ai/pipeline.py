"""Главный AI-pipeline v2 — с RAG из учебников.

Архитектура:

    photo_bytes
        ↓
    prepare_image() → base64 + resize
        ↓
    [параллельно]
      ┌──────────────────────────────┐
      │ extract_condition_text       │
      │   (легкий Claude vision OCR) │
      └──────────────────────────────┘
        ↓
    classify_topic(condition_text)  ← простая эвристика
        ↓
    embed_text(condition_text)      ← OpenAI embedding
        ↓
    find_similar_solutions(emb, topic, top_k=5)
        ↓
    cache_hit? (cosine_sim > 0.93)
      └── ДА → возврат готового решения (быстро, $0)
      └── НЕТ → продолжаем
        ↓
    build_rag_context(top 3 похожих)
        ↓
    solve_with_claude_vision(image, rag_context)
        ↓
    save_solution(condition, emb, solution, source="generated")
        ↓
    return solution
"""
import re
from typing import Awaitable, Callable, Optional

from loguru import logger

from app.ai.vision import prepare_image
from app.ai.claude import solve_with_claude_vision, extract_condition_text, fix_latex, fix_latex_strong
from app.ai.deepseek import solve_with_deepseek, solve_with_deepseek_plain, fix_latex_with_deepseek, generate_figure_with_deepseek
from app.ai.gemini import solve_with_gemini, solve_with_gemini_plain, fix_latex_with_gemini, generate_figure_with_gemini
from app.ai.latex_sanitize import sanitize_for_render, detect_latex_issues
from app.render.plain_pdf import render_plain_pdf
from app.render.figures import render_figures_in_latex, compile_figure, FIG_RE
from app.config import settings as _settings
from app.ai.embeddings import embed_text
from app.ai.retrieval import find_similar_solutions, save_solution, increment_usage
from app.render.latex_to_png import render_solution, render_verbatim
from app.analytics import log_event


# ── Solver router: переключаем free-mode между Gemini (быстро) и DeepSeek (медленно)
# через settings.free_mode_solver. Так можно откатиться одним .env без редеплоя кода
# (FREE_MODE_SOLVER=deepseek) — например если Gemini временно лагает или фильтрует.
def _solver_solve(condition_text: str, rag_context: str = "", user_hint: str = ""):
    if _settings.free_mode_solver == "deepseek":
        return solve_with_deepseek(condition_text, rag_context, user_hint)
    return solve_with_gemini(condition_text, rag_context, user_hint)


def _solver_plain(condition_text: str, rag_context: str = "", user_hint: str = ""):
    if _settings.free_mode_solver == "deepseek":
        return solve_with_deepseek_plain(condition_text, rag_context, user_hint)
    return solve_with_gemini_plain(condition_text, rag_context, user_hint)


def _solver_fix(broken_latex: str, error_log: str):
    if _settings.free_mode_solver == "deepseek":
        return fix_latex_with_deepseek(broken_latex, error_log)
    return fix_latex_with_gemini(broken_latex, error_log)


def _solver_figure(condition_text: str, user_hint: str = "", solution_excerpt: str = "", error: str = ""):
    if _settings.free_mode_solver == "deepseek":
        return generate_figure_with_deepseek(condition_text, user_hint, solution_excerpt, error)
    return generate_figure_with_gemini(condition_text, user_hint, solution_excerpt, error)


# Юзер ЯВНО просит рисунок (в caption к фото или в тексте).
_FIG_REQUEST_KW = (
    "нарису", "нарисов", "график", "изобраз", "начерт", "диаграмм", "чертёж", "чертеж",
    "построить график", "постройте график", "построй график", "схему", "схема",
    "plot", "graph",
)

# Авто-триггер «задаче НУЖЕН рисунок» по теме+содержанию (даже без явной просьбы) —
# цель: рисунки появляются всегда, где это уместно. Ключи специфичны, чтобы не
# форсить рисунок там, где он не нужен (напр. «найти производную»).
_FIG_NEEDED_BY_TOPIC: dict[str, tuple[str, ...]] = {
    "probability": ("плотност", "распределени", "гистограмм", "многоугольник распределени"),
    "matan": (
        "график", "исследова", "построить график", "постройте график", "площад",
        "касательн", "экстремум", "асимптот", "выпукл", "перегиб", "монотонн",
        "наибольшее и наименьшее", "фигур", "ограниченн лини",
    ),
    "physics": (
        "сил", "наклонн", "плоскост", "цеп", "контур", "кабел", "проводник", "линз",
        "луч", "зеркал", "волн", "маятник", "блок", "пружин", "траектори", "колеба",
        "падает", "брошен", "вектор", "схем",
    ),
    "discrete": (
        "граф", "дерев", "автомат", "схем", "функциональн", "вершин", "рёбр", "ребр",
        "орграф", "диаграмм", "гамильтон", "эйлер", "паросочет",
    ),
}


def _user_wants_figure(condition_text: str, user_hint: str = "") -> bool:
    blob = f"{user_hint}\n{condition_text}".lower()
    return any(k in blob for k in _FIG_REQUEST_KW)


def _task_needs_figure(condition_text: str, user_hint: str, topic: str) -> bool:
    """Нужен ли рисунок: явная просьба ИЛИ содержательный маркер по теме."""
    if _user_wants_figure(condition_text, user_hint):
        return True
    blob = f"{condition_text} {user_hint}".lower()
    return any(k in blob for k in _FIG_NEEDED_BY_TOPIC.get(topic, ()))


_FIGURE_GEN_ATTEMPTS = 3  # попыток сгенерировать КОМПИЛИРУЕМЫЙ рисунок (figure-вызов дёшев)


async def _ensure_figure(
    latex_clean: str, condition_text: str, user_hint: str, topic: str
) -> str:
    """Гарантия рисунка там, где он нужен. Если задача требует рисунка
    (_task_needs_figure), а солвер %%FIG не вставил — генерируем отдельным узким
    вызовом, ВАЛИДИРУЕМ компиляцией и ретраим с фидбэком ошибки. В решение
    дописываем только рисунок, который реально собрался (иначе не мусорим)."""
    if FIG_RE.search(latex_clean):
        return latex_clean  # солвер уже вставил рисунок
    if not _task_needs_figure(condition_text, user_hint, topic):
        return latex_clean
    err = ""
    for attempt in range(_FIGURE_GEN_ATTEMPTS):
        try:
            fig = await _solver_figure(condition_text, user_hint, latex_clean[:1500], err)
        except Exception as e:
            logger.warning(f"forced figure gen failed (попытка {attempt + 1}): {e}")
            break
        m = FIG_RE.search(fig or "")
        if not m:
            logger.warning(f"forced figure: пустой/невалидный ответ (попытка {attempt + 1})")
            continue
        # Валидируем компиляцией СРАЗУ (downstream render переиспользует кэш PNG).
        png = await compile_figure(m.group(1))
        if png:
            logger.info(f"figure: добавлен принудительно и собран (попытка {attempt + 1}, topic={topic})")
            return latex_clean.rstrip() + "\n\n" + fig + "\n"
        err = "рисунок не скомпилировался (pgfplots/tikz). Используй только числовые координаты."
    logger.warning(f"forced figure: не удалось получить компилируемый рисунок за {_FIGURE_GEN_ATTEMPTS} попыток")
    return latex_clean


_PLAIN_MARKERS = ("Задача:", "Решение:", "Ответ:")


def _is_plain_format(text: str) -> bool:
    """Контент в plain-формате (Unicode math, не LaTeX)?

    True если нет ни одного LaTeX-маркера (\\hd, \\frac, $$, \\(, \\[, \\begin{)
    и есть хотя бы один plain-маркер ("Задача:" / "Решение:" / "Ответ:").
    """
    if any(m in text for m in (r"\hd{", r"\ans{", "$$", r"\(", r"\[", r"\begin{", r"\frac")):
        return False
    return any(m in text for m in _PLAIN_MARKERS)


_FREE_FIX_ATTEMPTS = 2  # сколько раз пытаемся починить LaTeX моделью до plain-фолбэка


async def _render_with_autofix(
    latex: str, condition_text: str = "", telegram_id: int = 0
) -> tuple[dict, str]:
    """Гарантированный рендер PDF с авто-фиксами.

    free_mode (дешёвый путь):
      0) если контент уже plain — сразу ReportLab PDF.
      0.5) %%FIG-блоки → изолированная компиляция TikZ → \\includegraphics
           (ошибка рисунка не роняет решение — блок молча убирается).
      1) основной LaTeX-рендер (pdflatex, без -halt-on-error: одна ошибка не
         убивает весь документ).
      2) solver-fix LaTeX по логу ошибки → re-render (до _FREE_FIX_ATTEMPTS попыток).
      3) solver-plain (другой системный промпт) → ReportLab PDF.
    paid_mode (дорогой путь, использовался ранее):
      Haiku-fix → Sonnet-fix → verbatim.

    Метрики тиров пишутся через log_event (events.props) — видно частоту фолбэка
    и реальные причины падений (issues + дамп LaTeX) для последующей доводки.

    Sanitize применяется ВНУТРИ этой функции — покрывает все вызовы:
    cache-hit (старые записи в БД), pipeline cache-miss, OCR-fallback,
    плюс выходы fix-моделей.
    """
    latex = sanitize_for_render(latex)

    # Free-mode короткий путь: уже plain → ReportLab (рисунки plain не поддерживает).
    if _settings.free_mode and _is_plain_format(latex):
        rendered = await render_plain_pdf(latex)
        return rendered, latex

    # Рисунки: %%FIG-блоки компилируем изолированно → \includegraphics. Ошибка
    # отдельного рисунка не должна ронять всё решение (он молча опускается).
    try:
        latex, fig_ok, fig_failed = await render_figures_in_latex(latex)
        if fig_ok or fig_failed:
            logger.info(f"figures embedded: {fig_ok} ok / {fig_failed} failed")
            if fig_failed:
                log_event(telegram_id, "render_figure_fail", ok=fig_ok, failed=fig_failed)
    except Exception as e:
        logger.warning(f"figure preprocessing failed (non-fatal): {e}")
        latex = FIG_RE.sub("", latex)  # снять маркеры, чтобы сырой TikZ не ломал рендер

    # Tier 1 — основной LaTeX-рендер.
    rendered = await render_solution(latex)
    if rendered["pdf"] or not rendered.get("error"):
        log_event(telegram_id, "render_latex_ok")
        return rendered, latex

    # ── FREE-MODE: <solver>-fix (до N попыток) → <solver>-plain ──
    # Конкретный солвер (Gemini/DeepSeek) выбирается роутером по settings.free_mode_solver.
    if _settings.free_mode:
        err = rendered.get("error") or ""
        issues = detect_latex_issues(latex)
        logger.warning(
            f"render failed — {_settings.free_mode_solver}-fix (issues={issues or 'нет'})"
        )
        # Дамп падения для разбора РЕАЛЬНЫХ причин (events.props JSONB), не догадок.
        log_event(
            telegram_id, "render_failed",
            issues=issues, error_tail=err[-600:], latex_head=latex[:1500],
        )
        cur = latex
        for attempt in range(_FREE_FIX_ATTEMPTS):
            try:
                fixed = await _solver_fix(cur, err)
            except Exception as e:
                logger.warning(f"solver-fix attempt {attempt + 1} failed: {e}")
                break
            if not fixed:
                break
            fixed = sanitize_for_render(fixed)
            if fixed.strip() == cur.strip():
                break  # модель ничего не изменила — дальше бессмысленно
            re_rend = await render_solution(fixed)
            if re_rend["pdf"]:
                logger.info(f"{_settings.free_mode_solver}-fix succeeded (попытка {attempt + 1})")
                log_event(telegram_id, "render_latex_fixed", attempt=attempt + 1)
                return re_rend, fixed
            cur, err = fixed, (re_rend.get("error") or "")

        # Tier 3 — plain-формат через ReportLab. PDF будет всегда.
        if condition_text:
            logger.warning("LaTeX-путь исчерпан — переключаемся на plain-формат")
            try:
                plain = await _solver_plain(condition_text)
                if plain:
                    rendered_plain = await render_plain_pdf(plain)
                    if rendered_plain.get("pdf"):
                        logger.info("plain-PDF собран ✓")
                        log_event(telegram_id, "render_plain_fallback")
                        return rendered_plain, plain
            except Exception as e:
                logger.warning(f"plain-pipeline failed: {e}")
        log_event(telegram_id, "render_failed_final")
        return rendered, latex

    # ── PAID-MODE: legacy Haiku/Sonnet/verbatim chain ──
    logger.warning("render failed — Haiku-fix (paid-mode)")
    try:
        fixed = await fix_latex(latex, rendered["error"])
        if fixed:
            fixed = sanitize_for_render(fixed)
    except Exception as e:
        logger.warning(f"fix_latex (haiku) failed: {e}")
        fixed = None
    if fixed and fixed.strip() != latex.strip():
        rerendered = await render_solution(fixed)
        if rerendered["pdf"]:
            return rerendered, fixed
        latex, rendered = fixed, rerendered

    logger.warning("Haiku не вытянул — Sonnet-fix (paid)")
    try:
        fixed2 = await fix_latex_strong(latex, rendered.get("error") or "")
        if fixed2:
            fixed2 = sanitize_for_render(fixed2)
    except Exception as e:
        logger.warning(f"fix_latex_strong (sonnet) failed: {e}")
        return rendered, latex
    if fixed2 and fixed2.strip() != latex.strip():
        re2 = await render_solution(fixed2)
        if re2["pdf"]:
            return re2, fixed2
        rendered, latex = re2, fixed2

    logger.warning("Sonnet не вытянул — verbatim (paid)")
    try:
        vb = await render_verbatim(latex)
        if vb.get("pdf"):
            return vb, latex
    except Exception as e:
        logger.error(f"verbatim render failed: {e}")
    return rendered, latex

CACHE_HIT_THRESHOLD = 0.87       # понижено с 0.93 — больше попаданий в кэш generated
RAG_MIN_SIMILARITY = 0.65
RAG_TOP_K = 5
RAG_USE_TOP = 3

# Тип колбэка прогресс-статуса: async (text) -> None. None = статус не нужен.
StatusCb = Optional[Callable[[str], Awaitable[None]]]


async def _status(cb: StatusCb, text: str) -> None:
    """Безопасно дёрнуть колбэк статуса — ошибки редактирования не валят пайплайн."""
    if cb is None:
        return
    try:
        await cb(text)
    except Exception as e:
        logger.debug(f"status callback failed (non-fatal): {e}")


# Очистка от возможной markdown-обёртки ```latex ... ```
_FENCE_RE = re.compile(r"^```(?:latex|tex|math)?\s*\n?|\n?```\s*$", re.IGNORECASE)

# Маркеры устаревшего формата (HTML с эмодзи) — НЕ принимаем как cache hit
_LEGACY_MARKERS = ("<b>", "<i>", "<code>", "<pre>", "📝", "🎯", "🛠", "✅", "&lt;", "&gt;")
# Маркеры нашего LaTeX-формата
_LATEX_MARKERS = (r"\hd{", r"\ans{", r"\begin{", r"\frac", r"\int", "$$", r"\textbf")


def _clean_latex(text: str) -> str:
    """Снимает code-fence обёртку если Claude её добавил."""
    text = text.strip()
    text = _FENCE_RE.sub("", text)
    return text.strip()


# Все варианты math-окружений LaTeX. DeepSeek активно использует \(...\), \[...\]
# и align*, а не только $...$ — узкий регэксп их пропускал → сломанные кэшы.
_MATH_REGEXPS = [
    re.compile(r"(?<!\\)(?<!\$)\$([^\$\n]{1,500}?)\$(?!\$)", re.DOTALL),       # $...$
    re.compile(r"\$\$(.+?)\$\$", re.DOTALL),                                    # $$...$$
    re.compile(r"\\\((.+?)\\\)", re.DOTALL),                                    # \(...\)
    re.compile(r"\\\[(.+?)\\\]", re.DOTALL),                                    # \[...\]
    re.compile(r"\\begin\{(?:equation\*?|align\*?|gather\*?|multline\*?|displaymath)\}(.+?)\\end\{(?:equation\*?|align\*?|gather\*?|multline\*?|displaymath)\}", re.DOTALL),
]
_TEXT_WRAP_RE = re.compile(r"\\(?:text|mathrm|mbox|operatorname|textbf|textit)\{[^{}]*\}")
_CYR_RE = re.compile(r"[А-ЯЁа-яё]")


def _has_cyrillic_in_math(text: str) -> bool:
    """True если в каком-либо math-окружении есть кириллица БЕЗ \\text{...} обёртки.

    Покрывает $...$, $$...$$, \\(...\\), \\[...\\], align*, gather*, multline*,
    equation*, displaymath. Такие LaTeX падают на T2A с
    'Command \\cyrm invalid in math mode' / 'Bad math environment delimiter'.
    """
    for rx in _MATH_REGEXPS:
        for m in rx.finditer(text):
            body = _TEXT_WRAP_RE.sub("", m.group(1))
            if _CYR_RE.search(body):
                return True
    return False


def _is_valid_latex(text: str) -> bool:
    """Проверка валидности cached решения. Принимает либо LaTeX, либо plain-формат."""
    if not text:
        return False
    if any(m in text for m in _LEGACY_MARKERS):
        return False
    # Plain-формат (Unicode math) — принимаем сразу, если есть маркеры структуры.
    if any(m in text for m in _PLAIN_MARKERS):
        return True
    # LaTeX — должны быть маркеры и не должно быть кириллицы внутри math.
    if not any(m in text for m in _LATEX_MARKERS):
        return False
    if _has_cyrillic_in_math(text):
        return False
    return True


# Минимальная длина «настоящего решения». Меньше — почти гарантированно огрызок:
# - Gemini обрезанный thinking-budget'ом (видели 241 char)
# - DeepSeek/Gemini отказ на prompt-injection (~64 char: одна строка отказа)
# - Любой другой битый ответ
# Реальное мат-решение пошагово — минимум 500-800 chars, обычно 1500+.
# Под порог попадают и валидные ответы на тривиальные задачи (например «2+2=4»),
# но кэшировать их и не нужно: дешевле решить снова, чем мусорить БД.
_MIN_CACHEABLE_LEN = 500


def _is_cacheable_solution(latex_clean: str) -> bool:
    """True если решение стоит класть в БД-кэш как `source='generated'`.

    Defense in depth: даже если LaTeX-маркеры валидны и render собрал PDF,
    короткий ответ (огрызок / отказ / тривиальщина) больше навредит другим
    юзерам через cache-hit, чем сэкономит API-вызов. Не сохраняем.
    """
    if not latex_clean or len(latex_clean) < _MIN_CACHEABLE_LEN:
        return False
    # Должен пройти проверку формата (LaTeX-маркеры или plain-структура).
    if not _is_valid_latex(latex_clean):
        return False
    return True


async def solve_task_from_photo(
    photo_bytes: bytes,
    user_id: int,
    user_hint: str = "",
    on_status: StatusCb = None,
    skip_cache: bool = False,
    force_thinking: bool = False,
    mode: str = "premium",
) -> dict:
    """Главная точка входа из bot/handlers.py.

    mode:
        "standard" — solver по free_mode_solver (default Gemini) по тексту OCR (+RAG), без thinking, с кэшем.
        "premium"  — Sonnet 4.6 (vision) + extended thinking ВСЕГДА, без кэша.

    on_status — async-колбэк (text) для прогресс-статуса в чате (необязателен).

    Возвращает dict одного из видов:
      • Нужен выбор задачи (несколько задач на фото, подсказки нет):
          {"needs_choice": True, "task_ids": ["2851", "2852"]}
      • OCR не дал текста в standard-режиме (DeepSeek нечего решать):
          {"ocr_failed": True}
      • Готовое решение:
          {"latex": "...", "png": bytes | None}
    """
    logger.info(f"Pipeline start for user {user_id}")

    # 1) Подготовка фото
    image_b64, media_type = prepare_image(photo_bytes)

    # 2) Лёгкий OCR — распознать условие + список номеров задач на фото.
    # user_hint помогает выбрать нужную задачу при нескольких на фото.
    await _status(on_status, "📷 Распознаю условие…")
    condition_text, task_ids = await extract_condition_text(
        image_b64, media_type, user_hint=user_hint
    )

    # 2b) Несколько задач и подсказки нет → не решаем, просим выбрать.
    if not user_hint and len(task_ids) >= 2:
        logger.info(f"Multiple tasks, no hint → ask user. task_ids={task_ids}")
        return {"needs_choice": True, "task_ids": task_ids}

    # 3) OCR провалился/короткий.
    if not condition_text or len(condition_text) < 20:
        # standard решает по тексту OCR — без него DeepSeek нечего дать.
        if mode == "standard":
            logger.warning(f"OCR failed/short for user {user_id}, standard → ocr_failed")
            return {"ocr_failed": True}
        # premium (vision): fallback в простой vision-call без RAG
        logger.warning(f"OCR failed/short for user {user_id}, premium fallback no-RAG")
        await _status(on_status, "🧠 Решаю…")
        latex_raw = await solve_with_claude_vision(
            image_b64=image_b64,
            media_type=media_type,
            user_hint=user_hint,
            rag_context="",
        )
        latex_clean = _clean_latex(latex_raw)
        await _status(on_status, "🖼 Оформляю решение…")
        # Sanitize применяется внутри _render_with_autofix (централизованно).
        rendered, latex_clean = await _render_with_autofix(latex_clean, condition_text="")
        return {"latex": latex_clean, "png": rendered["preview_png"], "pdf": rendered["pdf"]}

    # 4) Классификация темы (для фильтрации retrieval)
    topic = classify_topic(condition_text)
    logger.info(f"Topic: {topic} | condition: {condition_text[:200]}")

    # 5) Эмбеддинг и поиск похожих (для RAG-контекста)
    await _status(on_status, "🔎 Ищу похожие задачи…")
    embedding = await embed_text(condition_text)
    similar_for_rag = await find_similar_solutions(
        embedding,
        topic=topic,
        top_k=RAG_TOP_K,
        min_similarity=RAG_MIN_SIMILARITY,
        only_generated=False,   # все источники (учебники + наши решения)
    )

    # 6) Cache hit ТОЛЬКО среди готовых решений (source='generated').
    #    Учебники без решений (только условие) — не отдаём как готовый ответ.
    #    skip_cache=True (например «перерешать») → не берём из кэша, решаем заново.
    # Кэш готовых решений — только для standard. Premium всегда решает заново
    # (гарантия Sonnet+thinking за 10 кредитов).
    use_cache = (not skip_cache) and (mode == "standard")
    cache_candidates = await find_similar_solutions(
        embedding,
        topic=topic,
        top_k=1,
        min_similarity=CACHE_HIT_THRESHOLD,
        only_generated=True,
    ) if use_cache else []
    if cache_candidates:
        hit = cache_candidates[0]
        cached_latex = _clean_latex(hit["solution_markdown"])
        if _is_valid_latex(cached_latex):
            logger.info(
                f"💎 CACHE HIT: sim={hit['cosine_sim']:.3f}, source={hit['source']}"
            )
            try:
                await increment_usage(hit["id"])
            except Exception as e:
                logger.warning(f"increment_usage failed: {e}")
            await _status(on_status, "💎 Нашёл готовое решение, оформляю…")
            cached_latex = await _ensure_figure(cached_latex, condition_text, user_hint, topic)
            rendered, cached_latex = await _render_with_autofix(cached_latex, condition_text=condition_text, telegram_id=user_id)
            return {"latex": cached_latex, "png": rendered["preview_png"], "pdf": rendered["pdf"]}
        else:
            logger.info(
                f"Cache hit rejected (legacy HTML format): sim={hit['cosine_sim']:.3f} "
                f"— regenerating in LaTeX"
            )

    # 7) Cache miss — собираем RAG-контекст и решаем через Claude
    rag_context = build_rag_context(similar_for_rag[:RAG_USE_TOP])

    # Выбор солвера по режиму.
    if mode == "standard":
        await _status(on_status, "🧠 Решаю…")
        solution_raw = await _solver_solve(
            condition_text=condition_text,
            rag_context=rag_context,
            user_hint=user_hint,
        )
    else:
        # premium: Sonnet + extended thinking ВСЕГДА (это и есть «премиум»).
        await _status(on_status, "🧠 Думаю над решением…")
        solution_raw = await solve_with_claude_vision(
            image_b64=image_b64,
            media_type=media_type,
            user_hint=user_hint,
            rag_context=rag_context,
            use_thinking=True,
        )
    # Sanitize ПЕРЕД сохранением в кэш — чтобы будущим юзерам не приходил
    # сломанный LaTeX, который потом будет провоцировать дорогую цепочку фиксов.
    latex_clean = sanitize_for_render(_clean_latex(solution_raw))
    # Гарантия рисунка, если юзер ЯВНО просил (модель часто игнорит просьбу).
    latex_clean = await _ensure_figure(latex_clean, condition_text, user_hint, topic)

    # 8) Сохраняем LaTeX в кэш для будущих юзеров — только если это реально решение.
    if _is_cacheable_solution(latex_clean):
        try:
            await save_solution(
                task_text=condition_text,
                task_latex=None,
                embedding=embedding,
                topic=topic,
                solution_markdown=latex_clean,
                source="generated",
                generated_for_user=user_id,
            )
        except Exception as e:
            logger.warning(f"save_solution failed (non-fatal): {e}")
    else:
        logger.warning(
            f"skip cache save: latex={len(latex_clean)} chars too short / invalid "
            f"(likely truncated answer or injection-refusal — not poisoning future users)"
        )

    # 9) Рендер PDF + PNG-превью (с кэшем по hash содержимого) + авто-фикс при ошибке
    await _status(on_status, "🖼 Оформляю решение…")
    rendered, latex_clean = await _render_with_autofix(latex_clean, condition_text=condition_text, telegram_id=user_id)

    logger.info(
        f"Pipeline done for user {user_id}: latex={len(latex_clean)} chars, "
        f"pdf={'OK' if rendered['pdf'] else 'FAIL'}"
    )
    return {"latex": latex_clean, "png": rendered["preview_png"], "pdf": rendered["pdf"]}


async def solve_task_from_text(
    condition_text: str,
    user_id: int,
    user_hint: str = "",
    on_status: StatusCb = None,
    skip_cache: bool = False,
) -> dict:
    """Решить задачу по ТЕКСТУ условия (без фото). Используется в free-mode и для
    text-input. Всегда стандартный режим (solver по settings.free_mode_solver,
    default Gemini), всегда с кэшем.

    Возвращает:
      • {"empty_input": True} если текст пустой/короткий
      • {"latex": "...", "png": ..., "pdf": ...} в успехе
    """
    logger.info(f"Pipeline (text) start for user {user_id}, len={len(condition_text)}")

    if not condition_text or len(condition_text.strip()) < 5:
        return {"empty_input": True}

    topic = classify_topic(condition_text)
    logger.info(f"Topic: {topic} | text: {condition_text[:200]}")

    await _status(on_status, "🔎 Ищу похожие задачи…")
    embedding = await embed_text(condition_text)
    similar_for_rag = await find_similar_solutions(
        embedding, topic=topic, top_k=RAG_TOP_K,
        min_similarity=RAG_MIN_SIMILARITY, only_generated=False,
    )

    use_cache = not skip_cache
    cache_candidates = await find_similar_solutions(
        embedding, topic=topic, top_k=1,
        min_similarity=CACHE_HIT_THRESHOLD, only_generated=True,
    ) if use_cache else []
    if cache_candidates:
        hit = cache_candidates[0]
        cached_latex = _clean_latex(hit["solution_markdown"])
        if _is_valid_latex(cached_latex):
            logger.info(f"💎 CACHE HIT (text): sim={hit['cosine_sim']:.3f}")
            try:
                await increment_usage(hit["id"])
            except Exception as e:
                logger.warning(f"increment_usage failed: {e}")
            await _status(on_status, "💎 Нашёл готовое решение, оформляю…")
            cached_latex = await _ensure_figure(cached_latex, condition_text, user_hint, topic)
            rendered, cached_latex = await _render_with_autofix(cached_latex, condition_text=condition_text, telegram_id=user_id)
            return {"latex": cached_latex, "png": rendered["preview_png"], "pdf": rendered["pdf"]}

    rag_context = build_rag_context(similar_for_rag[:RAG_USE_TOP])
    await _status(on_status, "🧠 Решаю…")
    solution_raw = await _solver_solve(
        condition_text=condition_text,
        rag_context=rag_context,
        user_hint=user_hint,
    )
    latex_clean = sanitize_for_render(_clean_latex(solution_raw))
    # Гарантия рисунка, если юзер ЯВНО просил (модель часто игнорит просьбу).
    latex_clean = await _ensure_figure(latex_clean, condition_text, user_hint, topic)

    if _is_cacheable_solution(latex_clean):
        try:
            await save_solution(
                task_text=condition_text, task_latex=None, embedding=embedding,
                topic=topic, solution_markdown=latex_clean,
                source="generated", generated_for_user=user_id,
            )
        except Exception as e:
            logger.warning(f"save_solution failed (non-fatal): {e}")
    else:
        logger.warning(
            f"skip cache save (text): latex={len(latex_clean)} chars too short / invalid"
        )

    await _status(on_status, "🖼 Оформляю решение…")
    rendered, latex_clean = await _render_with_autofix(latex_clean, condition_text=condition_text, telegram_id=user_id)

    logger.info(
        f"Pipeline (text) done for user {user_id}: latex={len(latex_clean)} chars, "
        f"pdf={'OK' if rendered['pdf'] else 'FAIL'}"
    )
    return {"latex": latex_clean, "png": rendered["preview_png"], "pdf": rendered["pdf"]}


def is_complex_task(task_text: str) -> bool:
    """Эвристика: задача требует extended thinking?

    Сложные задачи (доказательства, исследования, "при каких условиях") решаются
    с extended thinking budget — это дороже, но точнее.
    Простые (вычисления, стандартные методы) — без thinking.
    """
    text = task_text.lower()
    complex_markers = [
        "докажите", "доказать", "доказательств",
        "верно ли", "является ли", "следует ли",
        "найдите все", "найти все", "опишите все",
        "при каких", "для каких",
        "исследуйте", "исследовать",
        "показать что", "показать, что",
        "обосновать", "обоснуйте",
        "построить пример", "привести пример",
        "опровергнуть",
    ]
    return any(m in text for m in complex_markers)


def classify_topic(task_text: str) -> str:
    """Эвристическая классификация темы по ключевым словам."""
    text = task_text.lower()

    # Физика — проверяем ПЕРВОЙ, по специфичным фразам, чтобы сильный физ-сигнал
    # не утёк в matan/lin_alg. Ключи подобраны без пересечений с математикой
    # (НЕ берём "поле", "вектор", "индукц", "скорост" — они есть в мат-задачах).
    if any(kw in text for kw in [
        "ускорени", "сила тяжест", "сила трен", "сила упругост", "силы трен",
        "закон ньютон", "второй закон ньютон", "импульс тел", "импульс част",
        "кинетическ энерг", "потенциальн энерг", "механическ работ",
        "наклонн плоскост", "свободн паден", "брошен", "тело массой",
        "сила тока", "электрическ ток", "конденсатор", "резистор",
        "сопротивлен", "электродвижущ", "эдс", "магнитн пол", "магнитн индукц",
        "термодинамик", "теплоёмк", "теплот", "идеальн газ", "давлени газ",
        "оптик", "преломлен", "фокусн расстоян", "собирающ линз", "рассеивающ линз",
        "маятник", "период колебан", "длин волн", "амплитуд колебан",
    ]):
        return "physics"

    if any(kw in text for kw in [
        "групп", "подгрупп", "гомоморф", "изоморф", "циклич",
        "симметрическ", "перестановк", "коммутатор",
    ]):
        return "groups"

    if any(kw in text for kw in ["кольц", "поле", "идеал", "евклидов", "целостн"]):
        return "rings_fields"

    if any(kw in text for kw in [
        "многочлен", "полином", "корни уравнен", "теорема безу",
    ]):
        return "polynomials"

    if any(kw in text for kw in [
        "вероятн", "случайн велич", "случайной велич", "закон распределен",
        "распределение случайн", "математическое ожидан", "матожидан", "дисперси", "выборк",
        "гипотез", "корреляц", "биномиальн", "пуассон", "бернулли",
        "плотность вероятн", "доверительн интервал", "стандартное отклонен",
    ]):
        return "probability"

    if any(kw in text for kw in [
        # Комбинаторика
        "комбинатор", "сочетани", "размещени", "перестанов",
        "формула включен", "включений-исключен", "биноминальн", "биномиальн",
        "инъектив", "сюръектив", "сурьектив", "биектив",
        # Логика и булевы функции
        "булев", "высказыван", "полнота функц", "полнот булев",
        "конъюнктив", "дизъюнктив", "днф", "кнф", "тавтолог",
        # Базис Поста / Постовские классы (теорема о функциональной полноте).
        # Фразовые ключи — чтобы «полная система» в матане НЕ матчилось,
        # а «полная система функций» / «полные подсистемы функций» — да.
        # Регрессия: 31 мая user 589625614 ушёл в lin_alg → RAG из дискретки
        # не подключился → DeepSeek решал без контекста и висел.
        "полная систем функц", "полные подсистем", "минимальн полн подсистем",
        "штрих шеффер", "шеффер", "стрелка пирс", "пирса",
        # Класс Поста — поддерживаем и ASCII (T_0, T_1, T_L, T_M, T_S), и Unicode
        # (T₀, T₁) — юзеры часто пишут с индексами.
        "постов", "класс поста",
        "t_0", "t_1", "t_l", "t_m", "t_s",
        "t₀", "t₁",
        "₀-сохран", "₁-сохран",
        # Специфичные термины (без `функ`-суффикса — он не всегда стоит рядом).
        "самодвойств", "самодуальн",
        "монотонн булев", "класс монотонн",
        "сохраняющ нул", "сохраняющ едини",
        "линейн булев",
        # Типичные обороты в формулировках булевых задач. Падежи учитываем
        # явно — у нас простой substring-match, без regex.
        "вектор знач", "вектора знач", "вектором знач",
        "вектор истин", "вектора истин", "вектором истин",
        # Графы
        "граф", "вершин", "ребр", "эйлеров", "гамильтонов",
        "ориентированн", "неориентированн", "остовн дерев",
        "хроматическ", "паросочетан", "клик",
        # Автоматы и коды (есть в учебнике, но раньше попадали в matan)
        "автомат", "конечн автомат", "регулярн язык", "регулярн выражен",
        "детерминированн автомат",
        "хэмминг", "код грея", "декодирован", "кодирован",
        # Теория множеств / рекурренты
        "теория множеств", "отношение эквивалентн", "рекуррент",
        "теорема дилворт",
    ]):
        return "discrete"

    if any(kw in text for kw in [
        "матриц", "детерминант", "определитель", "слау",
        "ранг", "собственн", "линейн", "вектор", "пространств",
        "базис", "ортогональн", "квадратичн",
    ]):
        return "lin_alg"

    # дефолт — матан
    return "matan"


def build_rag_context(similar: list[dict]) -> str:
    """Формируем RAG-контекст из похожих задач для подсказки Claude.

    Берём task_text + solution_markdown (если есть). Урезаем чтобы влезло в контекст.
    """
    if not similar:
        return ""

    blocks = []
    for i, item in enumerate(similar, 1):
        task = (item.get("task_text") or "")[:800]
        sol = (item.get("solution_markdown") or "")[:1200]
        src = item.get("source", "учебник")
        sim = item.get("cosine_sim", 0.0)
        blocks.append(
            f"Пример {i} (из {src}, схожесть {sim:.2f}):\n"
            f"Задача: {task}\n"
            f"Решение/ответ:\n{sol}"
        )
    return "\n\n".join(blocks)
