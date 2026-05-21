"""Парсер учебника/задачника PDF → чанки → эмбеддинги → Supabase pgvector.

Pipeline:
    PDF
     ↓ pdf2image (poppler)
    PNG постранично (DPI 150)
     ↓ GPT-4o Vision через ProxyAPI
    JSON-чанки: задачи + теоремы + определения
     ↓ OpenAI text-embedding-3-small
    эмбеддинги (1536d)
     ↓ asyncpg
    INSERT INTO solutions (...)

Особенности:
- Чекпойнт каждые 10 страниц → можно прервать и продолжить.
- Параллельность через asyncio.Semaphore.
- --estimate показывает оценку $$ и страниц БЕЗ вызова API.
- Резюмирование с N-ной страницы (--start N) или диапазон (--end M).

Запуск (на VPS внутри контейнера):
    docker compose exec backend python scripts/parse_textbook.py \\
        textbooks/Demidovich.pdf --source "Демидович" --topic matan

    # Только estimate без вызова API
    docker compose exec backend python scripts/parse_textbook.py \\
        textbooks/Demidovich.pdf --source "Демидович" --topic matan --estimate

    # Только первые 20 страниц (smoke test)
    docker compose exec backend python scripts/parse_textbook.py \\
        textbooks/Demidovich.pdf --source "Демидович" --topic matan --end 20
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

# Доступ к app.* из backend
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from loguru import logger
from openai import AsyncOpenAI
from PIL import Image
from pdf2image import convert_from_path
from sqlalchemy import text
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.core.db import init_db, close_db, get_session
from app.ai.embeddings import embed_batch


# === Конфиг парсера ===
DPI = 150                       # 150 DPI — компромисс качество/cost
JPEG_QUALITY = 80
MAX_SIDE = 1568                 # лимит Anthropic/OpenAI для Vision
CONCURRENCY = 5                 # parallel API calls
COST_PER_PAGE_USD = 0.011       # для --estimate (≈$0.005 image + $0.005 output + $0.001 embed)
EMBED_BATCH = 64                # сколько чанков эмбеддим за раз


VISION_PROMPT = """Это страница из задачника/учебника по высшей математике (РФ, технический ВУЗ).
Извлеки ВСЕ задачи и теоретические блоки в JSON-массив.

Каждый элемент массива:
{
  "type": "task" | "task_with_solution" | "theorem" | "definition" | "example",
  "number": "номер задачи/теоремы/пункта если есть, иначе null",
  "text": "условие задачи или формулировка теоремы — текст с формулами в LaTeX внутри $...$",
  "solution": "решение если есть в исходнике (для type=task_with_solution или example) — иначе null",
  "answer": "числовой/символьный ответ если есть — иначе null"
}

ПРАВИЛА:
1. Извлекай ТОЛЬКО реальные задачи и теоретические блоки. Игнорируй заголовки разделов, оглавление, страницу с авторскими правами, пустые страницы.
2. Формулы — в LaTeX внутри одиночных $ для inline и $$ для блочных.
3. Если задача без номера (например, в учебнике-теории) — number=null.
4. ВАЖНО: верни ТОЛЬКО валидный JSON-массив. Без markdown-обёртки, без пояснений, без ```json. Просто [...].
5. Если страница НЕ содержит ни задач ни теории (титул, оглавление, пустая) — верни пустой массив [].
6. Не сокращай тексты задач — копируй полностью."""


@dataclass
class Chunk:
    chunk_type: str          # task / theorem / definition / example / task_with_solution
    number: str | None       # "1234" или "Теорема 4.7"
    text: str                # условие/формулировка (с LaTeX в $...$)
    solution: str | None     # решение если есть
    answer: str | None       # ответ если есть
    page: int                # номер страницы в PDF
    source: str              # "Демидович" / "Кострикин" / ...
    topic: str               # matan / lin_alg / groups / ...


def get_openai_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )


def pdf_page_to_b64(pdf_path: Path, page_num: int) -> str:
    """Конвертирует одну страницу PDF в JPEG base64."""
    images = convert_from_path(str(pdf_path), dpi=DPI, first_page=page_num, last_page=page_num)
    if not images:
        return ""
    img = images[0]
    # resize если слишком большая
    w, h = img.size
    if max(w, h) > MAX_SIDE:
        scale = MAX_SIDE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20))
async def vision_extract(client: AsyncOpenAI, image_b64: str, page_num: int) -> list[dict]:
    """Отправляет страницу в GPT-4o Vision, возвращает распарсенный JSON-массив чанков."""
    resp = await client.chat.completions.create(
        model=settings.parser_model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{image_b64}",
                    "detail": "high",
                }},
                {"type": "text", "text": VISION_PROMPT},
            ],
        }],
    )
    raw = resp.choices[0].message.content.strip()

    # снимаем возможную markdown-обёртку ```json ... ```
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            logger.warning(f"page {page_num}: not a list, got {type(data)}")
            return []
        return data
    except json.JSONDecodeError as e:
        logger.error(f"page {page_num}: JSON parse failed: {e}\nRaw: {raw[:200]}")
        return []


async def process_page(
    client: AsyncOpenAI,
    pdf_path: Path,
    page_num: int,
    source: str,
    topic: str,
    sem: asyncio.Semaphore,
) -> list[Chunk]:
    async with sem:
        try:
            b64 = await asyncio.to_thread(pdf_page_to_b64, pdf_path, page_num)
            if not b64:
                return []
            items = await vision_extract(client, b64, page_num)
            chunks: list[Chunk] = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                txt = (it.get("text") or "").strip()
                if not txt or len(txt) < 10:
                    continue
                chunks.append(Chunk(
                    chunk_type=it.get("type", "task"),
                    number=it.get("number"),
                    text=txt,
                    solution=it.get("solution"),
                    answer=it.get("answer"),
                    page=page_num,
                    source=source,
                    topic=topic,
                ))
            logger.info(f"page {page_num}: extracted {len(chunks)} chunks")
            return chunks
        except Exception as e:
            logger.exception(f"page {page_num}: failed: {e}")
            return []


async def save_chunks(chunks: list[Chunk]) -> int:
    """Эмбеддит и записывает в solutions. Возвращает число записанных строк."""
    if not chunks:
        return 0

    # Формируем текст для эмбеддинга: условие + решение если есть
    embed_inputs = [
        f"{c.text}\n\n{c.solution or ''}".strip() for c in chunks
    ]

    # Эмбеддинги пачкой
    all_embeddings: list[list[float]] = []
    for i in range(0, len(embed_inputs), EMBED_BATCH):
        batch = embed_inputs[i : i + EMBED_BATCH]
        embs = await embed_batch(batch)
        all_embeddings.extend(embs)

    # Bulk insert
    saved = 0
    async with get_session() as session:
        for chunk, emb in zip(chunks, all_embeddings):
            vec_str = "[" + ",".join(f"{v:.6f}" for v in emb) + "]"
            # task_text — условие; solution_markdown — решение если есть, иначе сам текст условия + ответ
            solution_md = chunk.solution or (
                f"<b>Условие.</b> {chunk.text}\n\n"
                + (f"<b>Ответ:</b> <code>{chunk.answer}</code>" if chunk.answer else "")
            )
            sql = """
                INSERT INTO solutions (
                    task_text, task_latex, embedding, topic, source, solution_markdown
                ) VALUES (
                    :task_text, :task_latex, CAST(:emb AS vector),
                    :topic, :source, :solution
                )
            """
            await session.execute(text(sql), {
                "task_text": chunk.text[:5000],
                "task_latex": None,
                "emb": vec_str,
                "topic": chunk.topic,
                "source": f"{chunk.source} (стр. {chunk.page})",
                "solution": solution_md[:8000],
            })
            saved += 1
        await session.commit()
    return saved


def get_total_pages(pdf_path: Path) -> int:
    """Грубо: pdf2image без конвертации не умеет, используем pdfinfo через pdf2image API."""
    from pdf2image.pdf2image import pdfinfo_from_path
    info = pdfinfo_from_path(str(pdf_path))
    return int(info.get("Pages", 0))


def load_checkpoint(pdf_path: Path) -> set[int]:
    cp_file = Path(".parser_cache") / f"{pdf_path.stem}.done.json"
    if not cp_file.exists():
        return set()
    return set(json.loads(cp_file.read_text()))


def save_checkpoint(pdf_path: Path, done_pages: set[int]):
    cp_dir = Path(".parser_cache")
    cp_dir.mkdir(exist_ok=True)
    cp_file = cp_dir / f"{pdf_path.stem}.done.json"
    cp_file.write_text(json.dumps(sorted(done_pages)))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf_path", help="PDF учебника/задачника")
    parser.add_argument("--source", required=True, help='Например, "Демидович"')
    parser.add_argument("--topic", required=True,
                        help="matan / lin_alg / groups / rings_fields / polynomials")
    parser.add_argument("--start", type=int, default=1, help="С какой страницы (1-based)")
    parser.add_argument("--end", type=int, default=None, help="По какую страницу включительно")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY,
                        help=f"Параллельных API-вызовов (default {CONCURRENCY})")
    parser.add_argument("--estimate", action="store_true",
                        help="Только показать оценку $$ и страниц, без вызова API")
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path).resolve()
    if not pdf_path.exists():
        logger.error(f"Файл не найден: {pdf_path}")
        return

    total = get_total_pages(pdf_path)
    end = args.end or total
    start = max(1, args.start)
    pages_to_process = list(range(start, end + 1))

    done = load_checkpoint(pdf_path)
    pending = [p for p in pages_to_process if p not in done]

    logger.info(
        f"PDF: {pdf_path.name} | total pages: {total} | "
        f"range: {start}-{end} | already done: {len(done)} | to process: {len(pending)}"
    )
    cost_usd = len(pending) * COST_PER_PAGE_USD
    logger.info(
        f"Estimated cost: ${cost_usd:.2f} (~{cost_usd * 100:.0f}₽) "
        f"at ${COST_PER_PAGE_USD:.4f}/page"
    )

    if args.estimate:
        logger.info("--estimate set, exiting without API calls.")
        return

    await init_db()
    client = get_openai_client()
    sem = asyncio.Semaphore(args.concurrency)

    t0 = time.time()
    total_saved = 0

    # обрабатываем батчами по 10 страниц чтобы чекпойнтить
    BATCH = 10
    for i in range(0, len(pending), BATCH):
        batch_pages = pending[i : i + BATCH]
        tasks = [
            process_page(client, pdf_path, p, args.source, args.topic, sem)
            for p in batch_pages
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        # flatten + save
        all_chunks = [c for chunks in results for c in chunks]
        if all_chunks:
            saved = await save_chunks(all_chunks)
            total_saved += saved
            logger.info(f"Saved {saved} chunks (batch {i//BATCH + 1})")

        # checkpoint
        done.update(batch_pages)
        save_checkpoint(pdf_path, done)

        elapsed = time.time() - t0
        progress = len(done - set(range(1, start))) / max(1, len(pending))
        logger.info(
            f"Progress: {i + len(batch_pages)}/{len(pending)} pages "
            f"| {total_saved} chunks total | elapsed {elapsed:.0f}s"
        )

    logger.info(f"DONE. Total chunks saved: {total_saved}, time: {time.time() - t0:.0f}s")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
