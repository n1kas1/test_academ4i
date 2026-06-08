"""OpenAI embeddings — text-embedding-3-small.

Используется для:
1. Эмбеддинг условия задачи юзера (для поиска похожих в pgvector)
2. Эмбеддинг чанков учебников (при парсинге)
"""
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=30.0,      # дефолт SDK 600с — при зависании шлюза копились бы запросы
            max_retries=1,     # внешний tenacity @retry уже даёт 3 попытки
        )
    return _client


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
async def embed_text(text: str) -> list[float]:
    """Получить эмбеддинг для одного текста. Возвращает list[float] длиной 1536."""
    client = get_client()
    response = await client.embeddings.create(
        model=settings.embedding_model,
        input=text[:8000],  # ограничение модели
    )
    return response.data[0].embedding


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Эмбеддинги пачкой (для парсинга учебников)."""
    client = get_client()
    response = await client.embeddings.create(
        model=settings.embedding_model,
        input=[t[:8000] for t in texts],
    )
    return [d.embedding for d in response.data]
