"""Главный AI-pipeline: фото → решение.

Архитектура (без Mathpix — Claude Vision делает OCR + reasoning одним вызовом):

    photo_bytes
        ↓
    prepare_image() → base64 (resize + JPEG compress)
        ↓
    Claude Vision (классифицирует тему по картинке через текст-блок,
                    либо мы используем простой keyword-классификатор на черновой OCR)
        ↓
    [параллельно] эмбеддинг описания → pgvector top-K похожих задач
        ↓
    Claude Vision + RAG-контекст похожих задач из учебников
        ↓
    Пошаговое решение в MarkdownV2
        ↓
    Сохранение в кэш solutions

Точные оптимизации:
- Кэширование по перцептивному хэшу фото (если идентичные фото — отдаём из БД).
- Кэширование по эмбеддингу текста условия (если cosine_sim > 0.93).
"""
from loguru import logger

from app.ai.vision import prepare_image
from app.ai.claude import solve_with_claude_vision
from app.ai.embeddings import embed_text
from app.ai.retrieval import find_similar_solutions, save_solution


async def solve_task_from_photo(
    photo_bytes: bytes,
    user_id: int,
    user_hint: str = "",
) -> str:
    """Главная точка входа из bot/handlers.py.

    Возвращает MarkdownV2-решение готовое к отправке в Telegram.
    """
    logger.info(f"Pipeline start for user {user_id}")

    # 1) Подготовка фото для Claude Vision
    image_b64, media_type = prepare_image(photo_bytes)

    # 2) Первый проход — Claude Vision БЕЗ RAG, чтобы:
    #    a) распознать условие в текст (для эмбеддинга и классификации темы)
    #    b) сразу получить базовое решение
    #
    # Чтобы не делать ДВА полных вызова Claude, мы делаем один с extended thinking
    # и используем его и для условия, и для решения. RAG-обогащение оставим на v2.
    solution = await solve_with_claude_vision(
        image_b64=image_b64,
        media_type=media_type,
        user_hint=user_hint,
        rag_context="",  # v1: без RAG. v2: добавим после первого парсинга учебников.
    )

    # 3) Сохраняем в кэш (текст условия извлекаем из решения по маркеру)
    # TODO v2: распарсить из solution блок "Условие:" → эмбеддинг → save_solution
    # Пока просто логируем — без сохранения, чтобы не засорять БД до первой итерации.

    logger.info(f"Pipeline done for user {user_id}: {len(solution)} chars")
    return solution


def classify_topic(task_text: str) -> str:
    """Простая классификация темы по ключевым словам (для retrieval-фильтра).

    Используется когда у нас уже есть текст условия (после Claude OCR).
    """
    text = task_text.lower()

    if any(kw in text for kw in ["группа", "подгрупп", "гомоморф", "изоморф", "циклич"]):
        return "groups"
    if any(kw in text for kw in ["кольц", "поле", "идеал"]):
        return "rings_fields"
    if any(kw in text for kw in ["многочлен", "полином", "корни"]):
        return "polynomials"
    if any(kw in text for kw in [
        "матриц", "детерминант", "определитель", "слау",
        "ранг", "собственн", "линейн", "вектор",
    ]):
        return "lin_alg"
    return "matan"


def build_rag_context(similar: list[dict]) -> str:
    """Формируем контекст из похожих задач для подсказки Claude."""
    if not similar:
        return ""
    blocks = []
    for i, item in enumerate(similar[:3], 1):
        blocks.append(
            f"=== Пример {i} (из {item.get('source', 'учебника')}) ===\n"
            f"Задача: {item['task_text']}\n"
            f"Решение:\n{item['solution_markdown']}\n"
        )
    return "\n\n".join(blocks)
