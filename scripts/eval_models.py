#!/usr/bin/env python3
"""Оценочный прогон солвер-моделей на 10 задачах. НЕ миграция — только тест.

Каждая задача прогоняется через каждую модель с ИДЕНТИЧНЫМ system-промптом
academ4i (app.ai.claude.SYSTEM_PROMPT) и ИДЕНТИЧНЫМ RAG-контекстом (топ-3 из
pgvector по cosine similarity к условию — переиспользуем боевые модули).

Вход солвера — ТЕКСТ условия (не фото): OCR/Haiku в проде остаётся при любой
миграции, а DeepSeek V4 — текстовая модель. Это честное сравнение «решательной»
способности. Единственная адаптация промпта: в user-сообщении вместо image-блока
идёт текст условия; system-промпт не меняется.

Транспорт адаптируется под провайдера:
  • anthropic → AsyncAnthropic (Sonnet, поддерживает extended thinking)
  • openai    → AsyncOpenAI на base_url ProxyAPI (Gemini Flash, DeepSeek V4)

thinking включается ТОЛЬКО для Sonnet и ТОЛЬКО когда is_complex_task(condition)
== True (боевая эвристика). Для openai-моделей параметра thinking нет — это
структурное неравенство, фиксируем в выводах.

Запуск и переменные окружения — см. test_results/RUN.md.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

# Боевые модули academ4i — единый источник промпта и RAG.
from app.config import settings
from app.ai.claude import SYSTEM_PROMPT
from app.ai.embeddings import embed_text
from app.ai.retrieval import find_similar_solutions
from app.ai.pipeline import (
    build_rag_context,
    is_complex_task,
    classify_topic,
    RAG_TOP_K,
    RAG_MIN_SIMILARITY,
    RAG_USE_TOP,
)

# ─────────────────────────────────────────────────────────────────────────
# КОНФИГ — проверь перед запуском
# ─────────────────────────────────────────────────────────────────────────

# provider: "anthropic" → AsyncAnthropic (нативный SDK, как в проде);
#           "openai"    → AsyncOpenAI на OpenAI-совместимый шлюз ProxyAPI.
# Шлюз ProxyAPI маршрутизирует по префиксу в имени модели:
#   gemini/<model>, openrouter/<provider>/<model>, anthropic/<model>, openai/<model>.
# Слаги и ставки сверены с публичным прайсом ProxyAPI на 2026-05-25.
#   • "deepseek-v4" НЕ существует — берём deepseek-chat-v3.1 через OpenRouter.
#   • gemini-3-flash-preview существует (152/910).
# max_tokens — необязательный override на модель (дефолт MAX_TOKENS=4000).
# R1 — reasoning-модель: thinking встроен (параметр thinking не передаём), но
# рассуждение ест output-токены, поэтому потолок выше (8000).
MODELS: dict[str, dict] = {
    "sonnet":      {"id": "claude-sonnet-4-6",                       "provider": "anthropic"},
    "deepseek":    {"id": "openrouter/deepseek/deepseek-chat-v3.1",  "provider": "openai"},
    "deepseek-r1": {"id": "openrouter/deepseek/deepseek-r1-0528",    "provider": "openai", "max_tokens": 8000},
    "gemini":      {"id": "gemini/gemini-3-flash-preview",           "provider": "openai"},
}

# OpenAI-совместимый шлюз ProxyAPI (кросс-провайдерная маршрутизация по префиксу).
# Отличается от settings.openai_base_url (тот — чистый OpenAI passthrough для эмбеддингов).
COMPAT_BASE_URL = "https://openai.api.proxyapi.ru/v1"

# Ставки ProxyAPI в ₽ за 1M токенов (input, output). Сверено с прайсом 2026-05-25.
#   • Sonnet, Gemini — фактические ставки из прайс-листа.
#   • DeepSeek идёт через OpenRouter (оригинал +25% OR +налоги +25% ProxyAPI) — в плоском
#     прайсе его нет, ставка ниже — ПЛЕЙСХОЛДЕР. Фактическое ₽ будет видно в дашборде после
#     прогона; точную стоимость можно пересчитать из results.json (usage-токены сохранены).
RATES_RUB_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6":                      (774.0, 3866.0),
    "gemini/gemini-3-flash-preview":          (152.0, 910.0),
    "openrouter/deepseek/deepseek-chat-v3.1": (30.0, 120.0),    # TODO: уточнить по дашборду
    "openrouter/deepseek/deepseek-r1-0528":   (45.0, 200.0),    # TODO: уточнить (R1 дороже chat)
}

MAX_TOKENS = 4000               # одинаковый потолок output для всех моделей
THINKING_BUDGET = 1500          # как в проде (claude.py)
PROJECTION_TASKS_PER_MONTH = 1000

ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = ROOT / "test_results" / "tasks"
RESULTS_DIR = ROOT / "test_results" / "results"
SUMMARY_MD = ROOT / "test_results" / "summary.md"
RESULTS_JSON = ROOT / "test_results" / "results.json"


# ─────────────────────────────────────────────────────────────────────────
# Чтение задач
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Task:
    num: str
    slug: str
    level: str
    topic: str
    expected: str
    condition: str


_COND_RE = re.compile(r"##\s*Условие\s*\n(.+)", re.S)


def _grab(text: str, label: str, default: str = "") -> str:
    m = re.search(rf"\*\*{label}:\*\*\s*(.+)", text)
    return m.group(1).strip() if m else default


def load_tasks() -> list[Task]:
    tasks: list[Task] = []
    for p in sorted(TASKS_DIR.glob("[0-9]*.md")):
        txt = p.read_text(encoding="utf-8")
        m = _COND_RE.search(txt)
        condition = m.group(1).strip() if m else ""
        num, _, slug = p.stem.partition("_")
        tasks.append(Task(
            num=num, slug=slug,
            level=_grab(txt, "Уровень"),
            topic=_grab(txt, "Тема"),
            expected=_grab(txt, "Ожидаемый ответ"),
            condition=condition,
        ))
    return tasks


# ─────────────────────────────────────────────────────────────────────────
# Сборка user-сообщения (адаптация боевой логики claude.py под text-режим)
# ─────────────────────────────────────────────────────────────────────────

def build_user_text(condition: str, rag_context: str) -> str:
    parts = [
        "Условие задачи (распознано с фото):\n"
        + condition
        + "\n\nРеши пошагово в указанном формате."
    ]
    if rag_context:
        parts.append(
            "\n━━━ ПОХОЖИЕ ЗАДАЧИ ИЗ УЧЕБНИКОВ (используй как референс стиля и метода) ━━━\n"
            + rag_context
            + "\n━━━ КОНЕЦ ━━━\n\nРеши именно эту задачу."
        )
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────
# RAG — идентичный для всех моделей в рамках одной задачи
# ─────────────────────────────────────────────────────────────────────────

async def fetch_rag(condition: str) -> tuple[str, float, str, int]:
    """Возвращает (rag_context, top_similarity, topic, n_hits)."""
    topic = classify_topic(condition)
    try:
        emb = await embed_text(condition)
        similar = await find_similar_solutions(
            emb, topic=topic, top_k=RAG_TOP_K,
            min_similarity=RAG_MIN_SIMILARITY, only_generated=False,
        )
        top_sim = similar[0]["cosine_sim"] if similar else 0.0
        return build_rag_context(similar[:RAG_USE_TOP]), top_sim, topic, len(similar)
    except Exception as e:
        logger.warning(f"RAG fetch failed (продолжаю без RAG): {e}")
        return "", 0.0, topic, 0


# ─────────────────────────────────────────────────────────────────────────
# Вызовы моделей
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class CallResult:
    text: str = ""
    in_tok: int = 0
    out_tok: int = 0
    seconds: float = 0.0
    thinking: bool = False
    error: str = ""


_anthropic_client = None
_openai_client = None


def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import AsyncAnthropic
        _anthropic_client = AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
        )
    return _anthropic_client


def _openai():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI
        # Кросс-провайдерный шлюз ProxyAPI (Gemini/DeepSeek), не путать с базой эмбеддингов.
        _openai_client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=COMPAT_BASE_URL,
        )
    return _openai_client


async def call_anthropic(model_id: str, user_text: str, thinking_on: bool) -> CallResult:
    kwargs: dict = {
        "model": model_id,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
    }
    if thinking_on:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}
    t0 = time.perf_counter()
    try:
        r = await _anthropic().messages.create(**kwargs)
    except Exception as e:
        return CallResult(seconds=time.perf_counter() - t0, thinking=thinking_on, error=repr(e))
    dt = time.perf_counter() - t0
    text = "".join(b.text for b in r.content if b.type == "text")
    return CallResult(
        text=text, in_tok=r.usage.input_tokens, out_tok=r.usage.output_tokens,
        seconds=dt, thinking=thinking_on,
    )


async def call_openai(model_id: str, user_text: str, max_tokens: int = MAX_TOKENS) -> CallResult:
    t0 = time.perf_counter()
    try:
        r = await _openai().chat.completions.create(
            model=model_id,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
        )
    except Exception as e:
        return CallResult(seconds=time.perf_counter() - t0, error=repr(e))
    dt = time.perf_counter() - t0
    msg_obj = r.choices[0].message
    text = msg_obj.content or ""
    # R1 и др. reasoning-модели через OpenRouter могут класть цепочку в .reasoning;
    # если финальный content пуст — сохраняем reasoning, чтобы было что оценивать.
    reasoning = getattr(msg_obj, "reasoning", None)
    if not text and reasoning:
        text = f"[reasoning, content пуст]\n{reasoning}"
    u = r.usage
    return CallResult(text=text, in_tok=u.prompt_tokens, out_tok=u.completion_tokens, seconds=dt)


def cost_rub(model_id: str, in_tok: int, out_tok: int) -> float:
    pin, pout = RATES_RUB_PER_MTOK.get(model_id, (0.0, 0.0))
    return (in_tok * pin + out_tok * pout) / 1_000_000


# ─────────────────────────────────────────────────────────────────────────
# Прогон
# ─────────────────────────────────────────────────────────────────────────

async def run() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks()
    if not tasks:
        raise SystemExit(f"Нет задач в {TASKS_DIR}")
    logger.info(f"Загружено задач: {len(tasks)}; моделей: {list(MODELS)}")

    records: list[dict] = []

    for task in tasks:
        logger.info(f"=== Задача {task.num} ({task.slug}, {task.level}/{task.topic}) ===")
        rag_context, top_sim, topic, n_hits = await fetch_rag(task.condition)
        thinking_flag = is_complex_task(task.condition)
        user_text = build_user_text(task.condition, rag_context)
        logger.info(f"RAG: topic={topic}, hits={n_hits}, top_sim={top_sim:.3f}; thinking(Sonnet)={thinking_flag}")

        # RAG-контекст — единый на задачу, сохраняем отдельно.
        (RESULTS_DIR / f"{task.num}_rag.md").write_text(
            f"# {task.num} {task.slug} — RAG-контекст (идентичен для всех моделей)\n\n"
            f"- topic (classify_topic): {topic}\n"
            f"- найдено похожих: {n_hits}\n"
            f"- top cosine similarity: {top_sim:.3f}\n\n"
            f"## Контекст, переданный моделям\n\n"
            f"{rag_context or '(пусто — похожих задач выше порога не найдено)'}\n",
            encoding="utf-8",
        )

        for name, cfg in MODELS.items():
            model_id = cfg["id"]
            if cfg["provider"] == "anthropic":
                res = await call_anthropic(model_id, user_text, thinking_on=thinking_flag)
            else:
                res = await call_openai(model_id, user_text, max_tokens=cfg.get("max_tokens", MAX_TOKENS))

            c = cost_rub(model_id, res.in_tok, res.out_tok)
            status = "ERROR" if res.error else "ok"
            logger.info(
                f"  [{name}] {status}: in={res.in_tok} out={res.out_tok} "
                f"≈{c:.2f}₽ {res.seconds:.1f}s thinking={res.thinking}"
            )

            (RESULTS_DIR / f"{task.num}_{name}.md").write_text(
                f"# {task.num} {task.slug} — {name} (`{model_id}`)\n\n"
                f"- **Уровень / тема:** {task.level} / {task.topic}\n"
                f"- **thinking:** {res.thinking}\n"
                f"- **input tokens:** {res.in_tok}\n"
                f"- **output tokens:** {res.out_tok}\n"
                f"- **cost:** {c:.3f} ₽\n"
                f"- **time:** {res.seconds:.2f} s\n"
                f"- **RAG top_sim:** {top_sim:.3f} (hits={n_hits})\n"
                + (f"- **ERROR:** `{res.error}`\n" if res.error else "")
                + f"\n## Условие\n\n{task.condition}\n\n"
                + f"## Эталонный ответ\n\n{task.expected}\n\n"
                + f"## Ответ модели (raw)\n\n```\n{res.text}\n```\n\n"
                + "## Оценка (заполняет Ярослав)\n\n"
                + "- Решено правильно? (да / нет / частично): \n"
                + "- Нотация ВШЭ? (да / частично / нет): \n"
                + "- Читаемо студенту? (да / нет): \n",
                encoding="utf-8",
            )

            records.append({
                "task": task.num, "slug": task.slug, "level": task.level,
                "topic": task.topic, "model": name, "model_id": model_id,
                "thinking": res.thinking, "in_tok": res.in_tok, "out_tok": res.out_tok,
                "cost_rub": round(c, 4), "seconds": round(res.seconds, 2),
                "error": res.error, "rag_top_sim": round(top_sim, 4),
            })

    RESULTS_JSON.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary(tasks, records)
    logger.info(f"Готово. Сводка: {SUMMARY_MD}")


def write_summary(tasks: list[Task], records: list[dict]) -> None:
    model_names = list(MODELS)

    def cell(task_num: str, model: str, key: str):
        for r in records:
            if r["task"] == task_num and r["model"] == model:
                return r
        return None

    lines: list[str] = []
    lines.append("# Сводка прогона моделей\n")
    lines.append(f"Задач: {len(tasks)} · Моделей: {len(model_names)} · "
                 f"max_tokens={MAX_TOKENS} (R1 — 8000) · thinking budget={THINKING_BUDGET}\n")
    lines.append("> Стоимость считается по фактическим usage-токенам × ставка ProxyAPI. "
                 "Проверь ставки и model-id в `scripts/eval_models.py` (раздел КОНФИГ).\n")
    lines.append("> Оговорки: thinking включён только у Sonnet и только по эвристике "
                 "`is_complex_task` (задачи 08, 10). У DeepSeek R1 reasoning встроенный "
                 "(thinking-параметр не передаётся, потолок 8000). System-промпт идентичен "
                 "для всех моделей (для R1 это не его рекомендованный режим — фиксируем как есть).\n")

    # Таблица 1 — стоимость (₽) по задачам
    lines.append("\n## Стоимость по задачам, ₽\n")
    lines.append("| # | задача | " + " | ".join(model_names) + " |")
    lines.append("|---|--------|" + "|".join(["---"] * len(model_names)) + "|")
    for t in tasks:
        row = [f"{t.num}", t.slug]
        for m in model_names:
            r = cell(t.num, m)
            row.append("ERR" if (r and r["error"]) else (f"{r['cost_rub']:.3f}" if r else "—"))
        lines.append("| " + " | ".join(row) + " |")

    # Таблица 2 — время (с) по задачам
    lines.append("\n## Время ответа по задачам, с\n")
    lines.append("| # | задача | " + " | ".join(model_names) + " |")
    lines.append("|---|--------|" + "|".join(["---"] * len(model_names)) + "|")
    for t in tasks:
        row = [f"{t.num}", t.slug]
        for m in model_names:
            r = cell(t.num, m)
            row.append("ERR" if (r and r["error"]) else (f"{r['seconds']:.1f}" if r else "—"))
        lines.append("| " + " | ".join(row) + " |")

    # Таблица 3 — агрегаты по моделям
    lines.append("\n## Итог по моделям\n")
    lines.append("| модель | model_id | Σ cost 10 задач, ₽ | avg cost/задача, ₽ | avg time, с | "
                 f"проекция @ {PROJECTION_TASKS_PER_MONTH}/мес, ₽ | ошибок |")
    lines.append("|---|---|---|---|---|---|---|")
    for m in model_names:
        rs = [r for r in records if r["model"] == m]
        ok = [r for r in rs if not r["error"]]
        n_err = sum(1 for r in rs if r["error"])
        total = sum(r["cost_rub"] for r in ok)
        avg_cost = total / len(ok) if ok else 0.0
        avg_time = sum(r["seconds"] for r in ok) / len(ok) if ok else 0.0
        proj = avg_cost * PROJECTION_TASKS_PER_MONTH
        mid = MODELS[m]["id"]
        lines.append(f"| {m} | `{mid}` | {total:.2f} | {avg_cost:.3f} | {avg_time:.1f} | "
                     f"{proj:.0f} | {n_err} |")
    lines.append("\n_Проекция = avg cost/задача × число задач/мес. Это стоимость ТОЛЬКО солвера; "
                 "OCR (Haiku) — одинаковая надбавка для всех вариантов миграции._\n")

    # Шаблон ручной оценки качества (Шаг 4)
    lines.append("\n## Оценка качества (заполняет Ярослав → Шаг 4)\n")
    lines.append("Детальные ответы — в `results/NN_<model>.md`. Сводные итоги:\n")
    lines.append("| модель | правильно (из 10) | правильная нотация (из 10) | читаемо (из 10) |")
    lines.append("|---|---|---|---|")
    for m in model_names:
        lines.append(f"| {m} |  |  |  |")

    SUMMARY_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(run())
