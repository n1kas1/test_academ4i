"""pgvector — поиск похожих задач в БД учебников.

Таблица solutions:
    id UUID
    task_text TEXT
    task_latex TEXT
    embedding vector(1536)
    topic TEXT  -- matan / lin_alg / groups / ...
    source TEXT -- "Демидович" / "Кострикин" / ...
    solution_markdown TEXT
    usage_count INT
    created_at TIMESTAMPTZ

SQL setup (см. backend/alembic/versions/):
    CREATE EXTENSION vector;
    CREATE TABLE solutions (...);
    CREATE INDEX ON solutions USING hnsw (embedding vector_cosine_ops);
"""
from loguru import logger
from sqlalchemy import text

from app.core.db import get_session


async def find_similar_solutions(
    embedding: list[float],
    topic: str | None = None,
    top_k: int = 5,
    min_similarity: float = 0.7,
) -> list[dict]:
    """Поиск топ-K похожих задач по cosine similarity, с фильтром по теме."""
    # pgvector: <=> = cosine distance (0..2). similarity = 1 - distance
    vec_str = "[" + ",".join(map(str, embedding)) + "]"

    sql = f"""
        SELECT
            id::text,
            task_text,
            task_latex,
            solution_markdown,
            topic,
            source,
            1 - (embedding <=> '{vec_str}'::vector) AS cosine_sim
        FROM solutions
        WHERE 1 - (embedding <=> '{vec_str}'::vector) > {min_similarity}
        {f"AND topic = '{topic}'" if topic else ""}
        ORDER BY embedding <=> '{vec_str}'::vector
        LIMIT {top_k}
    """

    async with get_session() as session:
        result = await session.execute(text(sql))
        rows = result.mappings().all()

    return [dict(r) for r in rows]


async def save_solution(
    task_text: str,
    task_latex: str,
    embedding: list[float],
    topic: str,
    solution_markdown: str,
    source: str = "generated",
):
    """Сохранить новое решение в кэш."""
    vec_str = "[" + ",".join(map(str, embedding)) + "]"

    sql = f"""
        INSERT INTO solutions (
            task_text, task_latex, embedding, topic, source, solution_markdown
        ) VALUES (
            :task_text, :task_latex, '{vec_str}'::vector, :topic, :source, :solution
        )
    """

    async with get_session() as session:
        await session.execute(
            text(sql),
            {
                "task_text": task_text,
                "task_latex": task_latex,
                "topic": topic,
                "source": source,
                "solution": solution_markdown,
            },
        )
        await session.commit()
    logger.info(f"Saved solution: topic={topic}, source={source}")
