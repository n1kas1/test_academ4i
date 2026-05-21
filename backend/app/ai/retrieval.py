"""pgvector retrieval — поиск похожих задач в кэше/учебниках Supabase.

Таблица solutions:
    id UUID
    task_text TEXT          — условие
    task_latex TEXT
    embedding vector(1536)  — text-embedding-3-small
    topic TEXT              — matan / lin_alg / groups / ...
    source TEXT             — "Демидович (стр. 42)" / "Кострикин (стр. 10)" / "generated"
    solution_markdown TEXT  — готовое решение (HTML для Telegram)
    usage_count INT
"""
from loguru import logger
from sqlalchemy import text

from app.core.db import get_session


def _vec_literal(emb: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in emb) + "]"


async def find_similar_solutions(
    embedding: list[float],
    topic: str | None = None,
    top_k: int = 5,
    min_similarity: float = 0.65,
) -> list[dict]:
    """Поиск top-K похожих задач по cosine similarity. Опционально фильтр по теме.

    Возвращает список dict с полями:
      id, task_text, task_latex, solution_markdown, topic, source, cosine_sim
    """
    vec = _vec_literal(embedding)

    base_sql = """
        SELECT
            id::text,
            task_text,
            task_latex,
            solution_markdown,
            topic,
            source,
            1 - (embedding <=> CAST(:emb AS vector)) AS cosine_sim
        FROM solutions
        WHERE 1 - (embedding <=> CAST(:emb AS vector)) > :min_sim
    """
    if topic:
        base_sql += " AND topic = :topic"
    base_sql += " ORDER BY embedding <=> CAST(:emb AS vector) LIMIT :top_k"

    params = {"emb": vec, "min_sim": min_similarity, "top_k": top_k}
    if topic:
        params["topic"] = topic

    async with get_session() as session:
        result = await session.execute(text(base_sql), params)
        rows = result.mappings().all()

    out = [dict(r) for r in rows]
    if out:
        logger.info(
            f"RAG retrieval: top_sim={out[0]['cosine_sim']:.3f}, "
            f"sources={[r['source'] for r in out[:3]]}"
        )
    else:
        logger.info(f"RAG retrieval: no matches (topic={topic}, min_sim={min_similarity})")
    return out


async def save_solution(
    task_text: str,
    task_latex: str | None,
    embedding: list[float],
    topic: str,
    solution_markdown: str,
    source: str = "generated",
    generated_for_user: int | None = None,
):
    """Сохранить новое решение в кэш."""
    vec = _vec_literal(embedding)

    sql = """
        INSERT INTO solutions (
            task_text, task_latex, embedding, topic, source,
            solution_markdown, generated_for_user
        ) VALUES (
            :task_text, :task_latex, CAST(:emb AS vector),
            :topic, :source, :solution, :gen_for
        )
    """
    async with get_session() as session:
        await session.execute(text(sql), {
            "task_text": task_text[:5000],
            "task_latex": task_latex,
            "emb": vec,
            "topic": topic,
            "source": source,
            "solution": solution_markdown[:8000],
            "gen_for": generated_for_user,
        })
        await session.commit()
    logger.info(f"Saved solution: topic={topic}, source={source}")


async def increment_usage(solution_id: str):
    """Увеличить usage_count при cache hit — для аналитики популярных задач."""
    sql = "UPDATE solutions SET usage_count = usage_count + 1 WHERE id = CAST(:id AS uuid)"
    async with get_session() as session:
        await session.execute(text(sql), {"id": solution_id})
        await session.commit()
