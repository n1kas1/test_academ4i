"""Парсер учебника PDF → чанки → эмбеддинги → pgvector.

Запуск:
    python scripts/parse_textbook.py textbooks/Demidovich.pdf --source "Демидович" --topic matan

Алгоритм:
1. Mathpix PDF API → структурированный markdown с LaTeX
2. Разбить на чанки: 1 задача = 1 чанк (детектим по нумерации "Задача 1.1" / "1.")
3. Для каждого чанка: эмбеддинг через OpenAI
4. Bulk insert в Supabase pgvector

TODO:
- Реализовать call Mathpix PDF API
- Парсинг markdown → чанки с метаданными (номер задачи, тема, страница)
- Batch insert через asyncpg
- Восстановление с checkpoint (если прервалось — продолжить)
"""
import argparse
import asyncio
import sys
from pathlib import Path

import httpx
from loguru import logger

# Добавляем backend в path для импорта app.*
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from app.config import settings
from app.ai.embeddings import embed_batch


MATHPIX_PDF_URL = "https://api.mathpix.com/v3/pdf"


async def upload_pdf_to_mathpix(pdf_path: Path) -> str:
    """Загрузить PDF в Mathpix → получить pdf_id для асинхронной обработки."""
    headers = {
        "app_id": settings.mathpix_app_id,
        "app_key": settings.mathpix_app_key,
    }
    options = {
        "conversion_formats": {"md": True},
        "math_inline_delimiters": ["$", "$"],
        "math_display_delimiters": ["$$", "$$"],
        "rm_spaces": True,
        "rm_fonts": True,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        with open(pdf_path, "rb") as f:
            files = {"file": (pdf_path.name, f, "application/pdf")}
            data = {"options_json": str(options).replace("'", '"')}
            resp = await client.post(MATHPIX_PDF_URL, headers=headers, files=files, data=data)
            resp.raise_for_status()
            return resp.json()["pdf_id"]


async def wait_for_mathpix_result(pdf_id: str) -> str:
    """Опросить статус Mathpix → когда completed, скачать markdown."""
    headers = {
        "app_id": settings.mathpix_app_id,
        "app_key": settings.mathpix_app_key,
    }
    status_url = f"https://api.mathpix.com/v3/pdf/{pdf_id}"
    md_url = f"https://api.mathpix.com/v3/pdf/{pdf_id}.md"

    async with httpx.AsyncClient(timeout=600.0) as client:
        # Ждать пока обработается (может быть несколько минут на большой PDF)
        for _ in range(60):
            status_resp = await client.get(status_url, headers=headers)
            status = status_resp.json().get("status")
            logger.info(f"Mathpix status: {status}")
            if status == "completed":
                break
            if status == "error":
                raise RuntimeError(f"Mathpix error: {status_resp.json()}")
            await asyncio.sleep(10)

        # Скачать markdown
        md_resp = await client.get(md_url, headers=headers)
        md_resp.raise_for_status()
        return md_resp.text


def split_into_chunks(markdown: str) -> list[dict]:
    """Разбить markdown учебника на чанки.

    Простая эвристика: разбиваем по заголовкам и нумерованным задачам.
    TODO: улучшить — пока берём по 1500 символов с overlap 200.
    """
    chunks = []
    chunk_size = 1500
    overlap = 200

    text = markdown
    pos = 0
    chunk_num = 0
    while pos < len(text):
        chunk_text = text[pos : pos + chunk_size]
        chunks.append({
            "chunk_num": chunk_num,
            "text": chunk_text,
            "position": pos,
        })
        pos += chunk_size - overlap
        chunk_num += 1
    return chunks


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf_path", help="Путь к PDF учебника")
    parser.add_argument("--source", required=True, help="Например, 'Демидович'")
    parser.add_argument("--topic", required=True, help="matan/lin_alg/groups/...")
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        logger.error(f"Файл не найден: {pdf_path}")
        return

    logger.info(f"Парсим {pdf_path.name} → source={args.source}, topic={args.topic}")

    # 1. Загрузка в Mathpix
    pdf_id = await upload_pdf_to_mathpix(pdf_path)
    logger.info(f"Mathpix pdf_id: {pdf_id}")

    # 2. Ждём результат
    markdown = await wait_for_mathpix_result(pdf_id)
    logger.info(f"Получили markdown: {len(markdown)} символов")

    # Сохраняем raw markdown для дебага
    cache_dir = Path(__file__).parent.parent / ".parser_cache"
    cache_dir.mkdir(exist_ok=True)
    (cache_dir / f"{pdf_path.stem}.md").write_text(markdown)

    # 3. Разбиваем на чанки
    chunks = split_into_chunks(markdown)
    logger.info(f"Получили {len(chunks)} чанков")

    # 4. Эмбеддинги пачками
    # TODO: batch insert в pgvector + retry/checkpoint
    BATCH = 50
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i : i + BATCH]
        texts = [c["text"] for c in batch]
        embeddings = await embed_batch(texts)
        logger.info(f"Embedded batch {i}-{i+len(batch)}")
        # TODO: insert в Supabase pgvector

    logger.info("Готово!")


if __name__ == "__main__":
    asyncio.run(main())
