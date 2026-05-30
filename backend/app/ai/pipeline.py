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
from app.ai.deepseek import solve_with_deepseek, solve_with_deepseek_plain, fix_latex_with_deepseek
from app.ai.latex_sanitize import sanitize_for_render
from app.render.plain_pdf import render_plain_pdf
from app.config import settings as _settings
from app.ai.embeddings import embed_text
from app.ai.retrieval import find_similar_solutions, save_solution, increment_usage
from app.render.latex_to_png import render_solution, render_verbatim


_PLAIN_MARKERS = ("Задача:", "Решение:", "Ответ:")


def _is_plain_format(text: str) -> bool:
    """Контент в plain-формате (Unicode math, не LaTeX)?

    True если нет ни одного LaTeX-маркера (\\hd, \\frac, $$, \\(, \\[, \\begin{)
    и есть хотя бы один plain-маркер ("Задача:" / "Решение:" / "Ответ:").
    """
    if any(m in text for m in (r"\hd{", r"\ans{", "$$", r"\(", r"\[", r"\begin{", r"\frac")):
        return False
    return any(m in text for m in _PLAIN_MARKERS)


async def _render_with_autofix(latex: str, condition_text: str = "") -> tuple[dict, str]:
    """Гарантированный рендер PDF с авто-фиксами.

    free_mode (дешёвый путь):
      0) если контент уже plain — сразу ReportLab PDF.
      1) основной LaTeX-рендер (pdflatex).
      2) DeepSeek-fix LaTeX по логу ошибки → re-render.
      3) DeepSeek-plain (тот же DeepSeek, другой системный промпт) → ReportLab PDF.
    paid_mode (дорогой путь, использовался ранее):
      Haiku-fix → Sonnet-fix → verbatim.

    Sanitize применяется ВНУТРИ этой функции — покрывает все вызовы:
    cache-hit (старые записи в БД), pipeline cache-miss, OCR-fallback,
    плюс выходы fix-моделей.
    """
    latex = sanitize_for_render(latex)

    # Free-mode короткий путь: уже plain → ReportLab.
    if _settings.free_mode and _is_plain_format(latex):
        rendered = await render_plain_pdf(latex)
        return rendered, latex

    # Tier 1 — основной LaTeX-рендер.
    rendered = await render_solution(latex)
    if rendered["pdf"] or not rendered.get("error"):
        return rendered, latex

    # ── FREE-MODE: DeepSeek-fix → DeepSeek-plain ──
    if _settings.free_mode:
        logger.warning("render failed — DeepSeek-fix LaTeX (free-mode)")
        try:
            fixed = await fix_latex_with_deepseek(latex, rendered["error"])
            if fixed:
                fixed = sanitize_for_render(fixed)
            if fixed and fixed.strip() != latex.strip():
                re_rend = await render_solution(fixed)
                if re_rend["pdf"]:
                    logger.info("DeepSeek-fix succeeded → PDF собран")
                    return re_rend, fixed
        except Exception as e:
            logger.warning(f"fix_latex_with_deepseek failed: {e}")

        # Tier 3 — plain-формат через ReportLab. PDF будет всегда.
        if condition_text:
            logger.warning("LaTeX-путь исчерпан — переключаемся на plain-формат")
            try:
                plain = await solve_with_deepseek_plain(condition_text)
                if plain:
                    rendered_plain = await render_plain_pdf(plain)
                    if rendered_plain.get("pdf"):
                        logger.info("plain-PDF собран ✓")
                        return rendered_plain, plain
            except Exception as e:
                logger.warning(f"plain-pipeline failed: {e}")
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
        "standard" — DeepSeek v3.1 по тексту OCR (+RAG), без thinking, с кэшем.
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
    logger.info(f"Topic: {topic} | condition: {condition_text[:120]}...")

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
            rendered, cached_latex = await _render_with_autofix(cached_latex, condition_text=condition_text)
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
        solution_raw = await solve_with_deepseek(
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

    # 8) Сохраняем LaTeX в кэш для будущих юзеров
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

    # 9) Рендер PDF + PNG-превью (с кэшем по hash содержимого) + авто-фикс при ошибке
    await _status(on_status, "🖼 Оформляю решение…")
    rendered, latex_clean = await _render_with_autofix(latex_clean, condition_text=condition_text)

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
    text-input. Всегда DeepSeek (standard-mode), всегда с кэшем.

    Возвращает:
      • {"empty_input": True} если текст пустой/короткий
      • {"latex": "...", "png": ..., "pdf": ...} в успехе
    """
    logger.info(f"Pipeline (text) start for user {user_id}, len={len(condition_text)}")

    if not condition_text or len(condition_text.strip()) < 5:
        return {"empty_input": True}

    topic = classify_topic(condition_text)
    logger.info(f"Topic: {topic} | text: {condition_text[:120]}…")

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
            rendered, cached_latex = await _render_with_autofix(cached_latex, condition_text=condition_text)
            return {"latex": cached_latex, "png": rendered["preview_png"], "pdf": rendered["pdf"]}

    rag_context = build_rag_context(similar_for_rag[:RAG_USE_TOP])
    await _status(on_status, "🧠 Решаю…")
    solution_raw = await solve_with_deepseek(
        condition_text=condition_text,
        rag_context=rag_context,
        user_hint=user_hint,
    )
    latex_clean = sanitize_for_render(_clean_latex(solution_raw))

    try:
        await save_solution(
            task_text=condition_text, task_latex=None, embedding=embedding,
            topic=topic, solution_markdown=latex_clean,
            source="generated", generated_for_user=user_id,
        )
    except Exception as e:
        logger.warning(f"save_solution failed (non-fatal): {e}")

    await _status(on_status, "🖼 Оформляю решение…")
    rendered, latex_clean = await _render_with_autofix(latex_clean, condition_text=condition_text)

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
        "комбинатор", "сочетани", "размещени", "булев", "высказыван",
        "рекуррент", "эйлеров", "гамильтонов", "вершин граф", "ребер граф",
        "ориентированн граф", "неориентированн граф", "остовн дерев",
        "теория множеств", "отношение эквивалентн",
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
