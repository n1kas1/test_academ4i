"""Haiku-классификатор темы запроса (математика/физика vs остальное).

Гейтит входящий текст ДО отправки в DeepSeek-солвер, чтобы бот не отвечал
на случайные вопросы / троллинг. ~0.1-0.3₽ за вызов.

Применяется к тексту (text-сообщение) и к OCR-расшифрованному условию (фото).
При ошибке Haiku — fail-open (пропускаем), чтобы не валить сервис из-за гейта.
"""
from loguru import logger

from app.config import settings
from app.ai.claude import get_client, estimate_cost_rub


_SYSTEM = """Ты — фильтр входящих сообщений в боте-решателе задач.
Бот решает ТОЛЬКО задачи по математике (матан, линал, алгебра, теорвер, дискретка)
и физике (механика, электродинамика, термодинамика, оптика, квантовая, СТО).

Получаешь текст сообщения. Решай: это задача (или вопрос по теории/методу) по
математике/физике, которую можно решить → "YES". Всё остальное (приветствия,
философия, тесты бота, троллинг, программирование, история, литература,
любой не-мат-физ-контент) → "NO".

Отвечай СТРОГО одним словом: YES или NO. Никаких пояснений, без пунктуации."""


async def is_math_or_physics(text: str) -> bool:
    """True если текст похож на задачу/вопрос по математике или физике."""
    if not text or len(text.strip()) < 3:
        return False
    client = get_client()
    try:
        r = await client.messages.create(
            model=settings.ocr_model,
            max_tokens=5,
            system=_SYSTEM,
            messages=[{"role": "user", "content": text[:1500]}],
        )
        raw = "".join(b.text for b in r.content if b.type == "text").strip().upper()
        verdict = raw.startswith("YES")
        u = r.usage
        logger.info(
            f"topic_gate [{settings.ocr_model}]: in={u.input_tokens}, out={u.output_tokens}, "
            f"≈{estimate_cost_rub(settings.ocr_model, u.input_tokens, u.output_tokens):.2f}₽, "
            f"verdict={'YES' if verdict else (raw or 'NO')}"
        )
        return verdict
    except Exception as e:
        logger.warning(f"topic_gate failed (fail-open): {e}")
        return True
