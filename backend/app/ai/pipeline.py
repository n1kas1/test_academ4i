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

from loguru import logger

from app.ai.vision import prepare_image
from app.ai.claude import solve_with_claude_vision, extract_condition_text
from app.ai.embeddings import embed_text
from app.ai.retrieval import find_similar_solutions, save_solution, increment_usage
from app.render.latex_to_png import render_latex_to_png

CACHE_HIT_THRESHOLD = 0.87       # понижено с 0.93 — больше попаданий в кэш generated
RAG_MIN_SIMILARITY = 0.65
RAG_TOP_K = 5
RAG_USE_TOP = 3


# Очистка от возможной markdown-обёртки ```latex ... ```
_FENCE_RE = re.compile(r"^```(?:latex|tex|math)?\s*\n?|\n?```\s*$", re.IGNORECASE)


def _clean_latex(text: str) -> str:
    """Снимает code-fence обёртку если Claude её добавил."""
    text = text.strip()
    text = _FENCE_RE.sub("", text)
    return text.strip()


async def solve_task_from_photo(
    photo_bytes: bytes,
    user_id: int,
    user_hint: str = "",
) -> dict:
    """Главная точка входа из bot/handlers.py.

    Возвращает dict:
      {
        "latex": "...",          # сырой LaTeX от Claude (для копирования юзером)
        "png": bytes | None,     # PNG-картинка решения (или None если рендер упал)
      }
    """
    logger.info(f"Pipeline start for user {user_id}")

    # 1) Подготовка фото
    image_b64, media_type = prepare_image(photo_bytes)

    # 2) Лёгкий OCR — распознать условие в текст для retrieval
    condition_text = await extract_condition_text(image_b64, media_type)

    # 3) Если OCR провалился — fallback в простой vision-call без RAG
    if not condition_text or len(condition_text) < 20:
        logger.warning(f"OCR failed/short for user {user_id}, fallback no-RAG")
        latex_raw = await solve_with_claude_vision(
            image_b64=image_b64,
            media_type=media_type,
            user_hint=user_hint,
            rag_context="",
        )
        latex_clean = _clean_latex(latex_raw)
        png = await render_latex_to_png(latex_clean)
        return {"latex": latex_clean, "png": png}

    # 4) Классификация темы (для фильтрации retrieval)
    topic = classify_topic(condition_text)
    logger.info(f"Topic: {topic} | condition: {condition_text[:120]}...")

    # 5) Эмбеддинг и поиск похожих (для RAG-контекста)
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
    cache_candidates = await find_similar_solutions(
        embedding,
        topic=topic,
        top_k=1,
        min_similarity=CACHE_HIT_THRESHOLD,
        only_generated=True,
    )
    if cache_candidates:
        hit = cache_candidates[0]
        logger.info(
            f"💎 CACHE HIT: sim={hit['cosine_sim']:.3f}, source={hit['source']}"
        )
        try:
            await increment_usage(hit["id"])
        except Exception as e:
            logger.warning(f"increment_usage failed: {e}")
        # В solution_markdown лежит LaTeX (наш generated кэш). Рендерим (с кэшем PNG).
        cached_latex = _clean_latex(hit["solution_markdown"])
        png = await render_latex_to_png(cached_latex)
        return {"latex": cached_latex, "png": png}

    # 7) Cache miss — собираем RAG-контекст и решаем через Claude
    rag_context = build_rag_context(similar_for_rag[:RAG_USE_TOP])

    # Router: простая задача → без extended thinking (~3₽);
    # сложная (доказательства, исследования) → с extended thinking (~5₽).
    use_thinking = is_complex_task(condition_text)
    logger.info(f"Router decision: complex={use_thinking}")

    solution_raw = await solve_with_claude_vision(
        image_b64=image_b64,
        media_type=media_type,
        user_hint=user_hint,
        rag_context=rag_context,
        use_thinking=use_thinking,
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

    # 9) Рендер PNG (с кэшем по hash содержимого)
    png = await render_latex_to_png(latex_clean)

    logger.info(f"Pipeline done for user {user_id}: latex={len(latex_clean)} chars, png={'OK' if png else 'FAIL'}")
    return {"latex": latex_clean, "png": png}


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
