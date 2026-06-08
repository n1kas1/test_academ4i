"""Gemini 2.5 Flash через ProxyAPI — основной солвер free-mode.

Зачем: DeepSeek v3.1 через OpenRouter+ProxyAPI выдаёт ≈30-80 токенов/сек, на
типичную мат-задачу уходит 20-50с — юзеры не дожидаются. Gemini 2.5 Flash через
тот же ProxyAPI выдаёт ≈150-250 ток/сек, та же задача — 5-15с.

Транспорт: **native Google API**, не OpenAI-совместимый. Маршрут через ProxyAPI:
  POST {gemini_base_url}/v1beta/models/{model}:generateContent?key=<api_key>

API-ключ — `openai_api_key` (один ProxyAPI-ключ обслуживает все шлюзы:
anthropic / openai / openai-compat / google).

Интерфейс зеркалит deepseek.py: те же `solve_with_gemini` / `solve_with_gemini_plain`
/ `fix_latex_with_gemini` — pipeline.py подменяет солвер одной строкой.
Защита от инъекций (`<TASK>`/`<HINT>`) переиспользуется из deepseek.py.
"""
from __future__ import annotations

import re
from typing import Any

import httpx
from loguru import logger

from app.config import settings
from app.ai.claude import SYSTEM_PROMPT
from app.ai.deepseek import (
    SYSTEM_PROMPT_PLAIN,
    _build_user_text,
    _build_user_text_plain,
    _DS_FIX_SYSTEM,
    FIGURE_SYSTEM_PROMPT,
    _build_figure_user_text,
    _extract_fig_block,
)


# Таймаут. Webhook fire-and-forget (200 OK сразу), так что Telegram-лимит не давит —
# можно ждать длинные решения. 120с покрывает тяжёлые задачи на полном бюджете вывода
# (16384 ток при ~150-250 ток/с ≈ 65-110с). Большинство задач отвечают за 2-15с.
_GEMINI_TIMEOUT_SEC = 120.0

# Стоимость в ₽ за 1M токенов (вход / выход). Плейсхолдер — уточнить по дашборду
# ProxyAPI после первых счётов. Gemini 2.5 Flash дешевле Sonnet, дороже DeepSeek.
_RUB_PER_MTOK = (25.0, 100.0)


def estimate_cost_rub(in_tok: int, out_tok: int) -> float:
    pin, pout = _RUB_PER_MTOK
    return (in_tok * pin + out_tok * pout) / 1_000_000


def _endpoint(model: str) -> str:
    """Native Google API: `/v1beta/models/{model}:generateContent`."""
    return f"{settings.gemini_base_url}/v1beta/models/{model}:generateContent"


async def _generate(system_prompt: str, user_text: str, *, log_tag: str) -> str:
    """Общий вызов Gemini API. Возвращает текст ответа (не пустой — иначе raise)."""
    url = _endpoint(settings.gemini_model)
    payload: dict[str, Any] = {
        # Native Google: system_instruction отдельно от contents.
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            # maxOutputTokens у Gemini 2.5 покрывает thinking + ответ. thinkingBudget=0
            # отключает reasoning → весь бюджет на ответ. 8192 ОБРЕЗАЛ длинные тервер/
            # доказательства на середине (finish=MAX_TOKENS, решение без «Ответа» →
            # юзер получал огрызок). 16384 даёт довести до конца. Flash поддерживает.
            "maxOutputTokens": 16384,
            # thinkingBudget=0 → отключить thinking. RAG-контекст у нас уже
            # есть как «подсказка», thinking дополнительной пользы не даёт.
            "thinkingConfig": {"thinkingBudget": 0},
            # temperature не задаём — дефолт оптимален для математики.
        },
    }
    params = {"key": settings.openai_api_key}

    logger.info(f"Gemini call start [{settings.gemini_model}] ({log_tag}): user_text_len={len(user_text)}")

    async with httpx.AsyncClient(timeout=_GEMINI_TIMEOUT_SEC) as client:
        resp = await client.post(url, params=params, json=payload)

    if resp.status_code != 200:
        # 4xx/5xx: логируем тело, бросаем — pipeline переключится на MSG_ERROR/MSG_TIMEOUT.
        body = resp.text[:500]
        logger.error(f"Gemini API {resp.status_code}: {body}")
        resp.raise_for_status()

    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        # Promptили заблокировали safety-фильтром или модель вернула пусто.
        feedback = data.get("promptFeedback") or {}
        logger.warning(f"Gemini empty candidates, feedback={feedback}")
        raise RuntimeError(f"Gemini returned no candidates (feedback={feedback})")

    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    finish_reason = candidates[0].get("finishReason", "?")

    usage = data.get("usageMetadata") or {}
    in_tok = usage.get("promptTokenCount", 0)
    out_tok = usage.get("candidatesTokenCount", 0)
    logger.info(
        f"Gemini solved [{settings.gemini_model}] ({log_tag}): "
        f"in={in_tok}, out={out_tok}, finish={finish_reason}, "
        f"≈{estimate_cost_rub(in_tok, out_tok):.2f}₽"
    )

    if not text:
        # finishReason помогает диагностировать: MAX_TOKENS / SAFETY / RECITATION / OTHER.
        logger.warning(f"Gemini empty text: finish={finish_reason}, in={in_tok}, out={out_tok}")
        raise RuntimeError(f"Gemini returned empty text (finish={finish_reason})")
    return text


async def solve_with_gemini(
    condition_text: str,
    rag_context: str = "",
    user_hint: str = "",
) -> str:
    """Решить задачу через Gemini 2.5 Flash в LaTeX-формате (зеркалит solve_with_deepseek)."""
    user_text = _build_user_text(condition_text, rag_context, user_hint)
    return await _generate(SYSTEM_PROMPT, user_text, log_tag="solve")


async def solve_with_gemini_plain(
    condition_text: str,
    rag_context: str = "",
    user_hint: str = "",
) -> str:
    """Решить задачу в plain-формате (Unicode math, без LaTeX). Зеркалит solve_with_deepseek_plain."""
    user_text = _build_user_text_plain(condition_text, rag_context, user_hint)
    return await _generate(SYSTEM_PROMPT_PLAIN, user_text, log_tag="plain")


async def generate_figure_with_gemini(
    condition_text: str, user_hint: str = "", solution_excerpt: str = "", error: str = ""
) -> str:
    """Сгенерировать ТОЛЬКО рисунок через Gemini. Возвращает %%FIG-блок или ''.
    Зеркалит generate_figure_with_deepseek (общий промпт/хелперы из deepseek.py)."""
    user_text = _build_figure_user_text(condition_text, user_hint, solution_excerpt, error)
    out = await _generate(FIGURE_SYSTEM_PROMPT, user_text, log_tag="figure")
    return _extract_fig_block(out)


async def fix_latex_with_gemini(broken_latex: str, error_log: str) -> str:
    """Починить невалидный LaTeX через Gemini. Зеркалит fix_latex_with_deepseek."""
    user_msg = (
        f"Ошибка pdflatex:\n{(error_log or '')[-1500:]}\n\n"
        f"LaTeX-фрагмент (почини и верни целиком):\n{broken_latex}"
    )
    out = await _generate(_DS_FIX_SYSTEM, user_msg, log_tag="fix")
    # Снимаем code-fence обёртку если попала (как в deepseek-варианте).
    fence = re.search(r"```(?:latex)?\s*(.*?)```", out, re.DOTALL)
    if fence:
        out = fence.group(1).strip()
    return out or broken_latex
