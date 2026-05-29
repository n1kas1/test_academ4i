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
from app.ai.claude import solve_with_claude_vision, extract_condition_text, fix_latex
from app.ai.deepseek import solve_with_deepseek
from app.ai.embeddings import embed_text
from app.ai.retrieval import find_similar_solutions, save_solution, increment_usage
from app.render.latex_to_png import render_solution


async def _render_with_autofix(latex: str) -> tuple[dict, str]:
    """Рендер; при ошибке компиляции pdflatex — один авто-фикс LaTeX (Haiku) и повтор.

    Возвращает (rendered, final_latex). Если фикс удался — final_latex исправленный
    (он же уйдёт в кнопку «Показать LaTeX»); иначе исходный (для текстового фолбэка).
    """
    rendered = await render_solution(latex)
    if rendered["pdf"] or not rendered.get("error"):
        return rendered, latex
    logger.warning("render failed — пробую авто-фикс LaTeX через Haiku")
    try:
        fixed = await fix_latex(latex, rendered["error"])
    except Exception as e:
        logger.warning(f"fix_latex failed: {e}")
        return rendered, latex
    if not fixed or fixed.strip() == latex.strip():
        return rendered, latex
    rerendered = await render_solution(fixed)
    if rerendered["pdf"]:
        logger.info("LaTeX авто-фикс удался → PDF собран")
        return rerendered, fixed
    logger.warning("авто-фикс не помог — текстовый фолбэк")
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


def _is_valid_latex(text: str) -> bool:
    """Проверка что cached решение — наш текущий LaTeX-формат, а не устаревший HTML."""
    if not text:
        return False
    if any(m in text for m in _LEGACY_MARKERS):
        return False
    return any(m in text for m in _LATEX_MARKERS)


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
        rendered, latex_clean = await _render_with_autofix(latex_clean)
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
            rendered, cached_latex = await _render_with_autofix(cached_latex)
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
    latex_clean = _clean_latex(solution_raw)

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
    rendered, latex_clean = await _render_with_autofix(latex_clean)

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
            rendered, cached_latex = await _render_with_autofix(cached_latex)
            return {"latex": cached_latex, "png": rendered["preview_png"], "pdf": rendered["pdf"]}

    rag_context = build_rag_context(similar_for_rag[:RAG_USE_TOP])
    await _status(on_status, "🧠 Решаю…")
    solution_raw = await solve_with_deepseek(
        condition_text=condition_text,
        rag_context=rag_context,
        user_hint=user_hint,
    )
    latex_clean = _clean_latex(solution_raw)

    try:
        await save_solution(
            task_text=condition_text, task_latex=None, embedding=embedding,
            topic=topic, solution_markdown=latex_clean,
            source="generated", generated_for_user=user_id,
        )
    except Exception as e:
        logger.warning(f"save_solution failed (non-fatal): {e}")

    await _status(on_status, "🖼 Оформляю решение…")
    rendered, latex_clean = await _render_with_autofix(latex_clean)

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
