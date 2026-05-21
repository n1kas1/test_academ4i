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
from loguru import logger

from app.ai.vision import prepare_image
from app.ai.claude import solve_with_claude_vision, extract_condition_text
from app.ai.embeddings import embed_text
from app.ai.retrieval import find_similar_solutions, save_solution, increment_usage

CACHE_HIT_THRESHOLD = 0.93
RAG_MIN_SIMILARITY = 0.65
RAG_TOP_K = 5
RAG_USE_TOP = 3


async def solve_task_from_photo(
    photo_bytes: bytes,
    user_id: int,
    user_hint: str = "",
) -> str:
    """Главная точка входа из bot/handlers.py."""
    logger.info(f"Pipeline start for user {user_id}")

    # 1) Подготовка фото
    image_b64, media_type = prepare_image(photo_bytes)

    # 2) Лёгкий OCR — распознать условие в текст для retrieval
    condition_text = await extract_condition_text(image_b64, media_type)

    # 3) Если OCR провалился — fallback в простой vision-call без RAG
    if not condition_text or len(condition_text) < 20:
        logger.warning(f"OCR failed/short for user {user_id}, fallback no-RAG")
        return await solve_with_claude_vision(
            image_b64=image_b64,
            media_type=media_type,
            user_hint=user_hint,
            rag_context="",
        )

    # 4) Классификация темы (для фильтрации retrieval)
    topic = classify_topic(condition_text)
    logger.info(f"Topic: {topic} | condition: {condition_text[:120]}...")

    # 5) Эмбеддинг и поиск похожих
    embedding = await embed_text(condition_text)
    similar = await find_similar_solutions(
        embedding,
        topic=topic,
        top_k=RAG_TOP_K,
        min_similarity=RAG_MIN_SIMILARITY,
    )

    # 6) Cache hit — возвращаем готовое решение мгновенно
    if similar and similar[0]["cosine_sim"] > CACHE_HIT_THRESHOLD:
        hit = similar[0]
        logger.info(
            f"💎 CACHE HIT: sim={hit['cosine_sim']:.3f}, source={hit['source']}"
        )
        try:
            await increment_usage(hit["id"])
        except Exception as e:
            logger.warning(f"increment_usage failed: {e}")
        return hit["solution_markdown"]

    # 7) Cache miss — собираем RAG-контекст и решаем через Claude
    rag_context = build_rag_context(similar[:RAG_USE_TOP])
    solution = await solve_with_claude_vision(
        image_b64=image_b64,
        media_type=media_type,
        user_hint=user_hint,
        rag_context=rag_context,
    )

    # 8) Сохраняем в кэш для будущих юзеров
    try:
        await save_solution(
            task_text=condition_text,
            task_latex=None,
            embedding=embedding,
            topic=topic,
            solution_markdown=solution,
            source="generated",
            generated_for_user=user_id,
        )
    except Exception as e:
        logger.warning(f"save_solution failed (non-fatal): {e}")

    logger.info(f"Pipeline done for user {user_id}: {len(solution)} chars")
    return solution


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
