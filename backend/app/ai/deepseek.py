"""DeepSeek v3.1 через OpenRouter (OpenAI-совместимый шлюз ProxyAPI).

Текстовый солвер standard-режима: получает РАСПОЗНАННЫЙ Haiku-OCR текст условия
(+ RAG-контекст), не фото — DeepSeek v3.1 текстовая модель. System-промпт тот же,
что у Sonnet (app.ai.claude.SYSTEM_PROMPT), чтобы формат вывода (LaTeX) совпадал.

Транспорт идентичен адаптеру из scripts/eval_models.py.
"""
import html

from openai import AsyncOpenAI
from loguru import logger

from app.config import settings
from app.ai.claude import SYSTEM_PROMPT

_client: AsyncOpenAI | None = None

# Стоимость в ₽ за 1M токенов (вход / выход). DeepSeek идёт через OpenRouter
# (оригинал +25% OR +налоги +25% ProxyAPI) — в плоском прайсе его нет, это
# ПЛЕЙСХОЛДЕР. Уточнить по дашборду ProxyAPI; факт виден в логах списания.
_RUB_PER_MTOK = (30.0, 120.0)

# Output-лимит. На free-mode подняли с 4096 — DeepSeek дешёвый, можно дать
# длинные пошаговые решения без обрезов. Input не лимитируем (контекст 128К).
MAX_TOKENS = 8192


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_compat_base_url,
        )
    return _client


def estimate_cost_rub(in_tok: int, out_tok: int) -> float:
    pin, pout = _RUB_PER_MTOK
    return (in_tok * pin + out_tok * pout) / 1_000_000


def _build_user_text(condition_text: str, rag_context: str, user_hint: str) -> str:
    """Сборка user-сообщения в text-режиме (зеркалит claude.solve_with_claude_vision,
    но вместо image-блока — текст условия)."""
    parts = [
        "Условие задачи (распознано с фото):\n"
        + condition_text
        + "\n\nРеши пошагово в указанном формате."
    ]
    if user_hint:
        parts.append(f"\nКонтекст от студента: {html.escape(user_hint)}")
    if rag_context:
        parts.append(
            "\n━━━ ПОХОЖИЕ ЗАДАЧИ ИЗ УЧЕБНИКОВ (используй как референс стиля и метода) ━━━\n"
            + rag_context
            + "\n━━━ КОНЕЦ ━━━\n\nРеши именно эту задачу."
        )
    return "\n".join(parts)


async def solve_with_deepseek(
    condition_text: str,
    rag_context: str = "",
    user_hint: str = "",
) -> str:
    """Решить задачу текстом через DeepSeek v3.1. Возвращает LaTeX-решение."""
    client = get_client()
    user_text = _build_user_text(condition_text, rag_context, user_hint)

    response = await client.chat.completions.create(
        model=settings.deepseek_model,
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
    )

    text = response.choices[0].message.content or ""
    usage = response.usage
    in_tok = getattr(usage, "prompt_tokens", 0)
    out_tok = getattr(usage, "completion_tokens", 0)
    logger.info(
        f"DeepSeek solved [{settings.deepseek_model}]: "
        f"in={in_tok}, out={out_tok}, ≈{estimate_cost_rub(in_tok, out_tok):.2f}₽"
    )
    return text.strip()
