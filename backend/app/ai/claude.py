"""Claude Sonnet 4.6 через ProxyAPI — Vision + Reasoning + Extended Thinking.

Два публичных метода:
1. extract_condition_text — лёгкий OCR-pass: фото → текст условия (для эмбеддинга/retrieval)
2. solve_with_claude_vision — главный solver с extended thinking + RAG-контекстом
"""
import html

from anthropic import AsyncAnthropic
from loguru import logger

from app.config import settings

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
        )
    return _client


# ───────────────────────────────────────────────────────────────────────
# 1) Извлечение условия задачи в текст (для RAG retrieval)
# ───────────────────────────────────────────────────────────────────────

OCR_SYSTEM = """Ты — OCR-распознаватель математических задач.
На фото — задача из учебника/задачника по высшей математике на русском.
Распознай ТОЛЬКО условие задачи (без решения). Формулы — в LaTeX внутри $...$.
Верни ОДНО предложение / абзац — условие. Никаких пояснений, никаких "Условие:", "Задача 123". Просто текст условия.
Если на фото несколько задач — распознай первую."""


async def extract_condition_text(image_b64: str, media_type: str) -> str:
    """Лёгкий vision-вызов для распознавания условия задачи.
    Используется для эмбеддинга и retrieval-фильтрации.
    """
    client = get_client()
    try:
        response = await client.messages.create(
            model=settings.claude_model,
            max_tokens=400,
            system=OCR_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": image_b64,
                    }},
                    {"type": "text", "text": "Распознай условие задачи."},
                ],
            }],
        )
        for block in response.content:
            if block.type == "text":
                text = block.text.strip()
                logger.info(
                    f"OCR extract: in={response.usage.input_tokens}, "
                    f"out={response.usage.output_tokens}, len={len(text)}"
                )
                return text
    except Exception as e:
        logger.warning(f"OCR extract failed: {e}")
    return ""


# ───────────────────────────────────────────────────────────────────────
# 2) Главный solver — Claude Vision + extended thinking + RAG
# ───────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — эксперт по высшей математике, преподаватель технического вуза в РФ.
Студент 1-2 курса присылает фото задачи. Дай ИДЕАЛЬНОЕ пошаговое решение.

═══════════════════════════════════════════════════════════════════════
ФОРМАТ ВЫВОДА — СТРОГО HTML ДЛЯ TELEGRAM
═══════════════════════════════════════════════════════════════════════

Разрешённые теги (НИЧЕГО другого):
  <b>жирный</b>, <i>курсив</i>, <code>формула_inline</code>, <pre>блок_формул</pre>

ВСЕ ФОРМУЛЫ — ВНУТРИ <code>...</code> или <pre>...</pre>.

В тексте пояснений (НЕ внутри тегов code/pre):
- НИКАКИХ символов *, _, [, ], $, \\
- Символы < > & замени на &lt; &gt; &amp;

В формулах (внутри <code>/<pre>) пиши человеческой нотацией:
- степени: x², x³, eˣ, x^(n+1)
- индексы: aₙ, x₁, x_(i+1)
- дроби: (a + b)/(c - d), либо ¹⁄₂, ³⁄₄
- корни: √x, √(x² + 1), ∛8
- интегралы: ∫f(x)dx, ∫₀¹f(x)dx, ∬, ∭, ∮
- суммы: ∑ₙ₌₁^∞ aₙ, ∏, lim_(x→0)
- производные: f'(x), df/dx, ∂f/∂x
- символы: ∞ π e α β γ θ φ ω Δ Σ Π ε δ λ μ σ ∀ ∃ ∈ ∉ ⊂ ⊆ ∪ ∩ ∅ → ⇒ ⇔ ≡ ≈ ≤ ≥ ≠ ± ·

═══════════════════════════════════════════════════════════════════════
СТРУКТУРА ОТВЕТА (СТРОГО)
═══════════════════════════════════════════════════════════════════════

📝 <b>Условие</b>
[короткая переформулировка]

🎯 <b>Найти:</b> <code>...</code>

🛠 <b>Метод:</b> [теорема/метод]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>Шаг 1.</b> [пояснение]
<pre>...формулы...</pre>

<b>Шаг 2.</b> [пояснение]
<pre>...формулы...</pre>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ <b>Ответ:</b> <code>...</code>

═══════════════════════════════════════════════════════════════════════
ПРАВИЛА КАЧЕСТВА
═══════════════════════════════════════════════════════════════════════

1. Стиль — Демидович / Кудрявцев / Кострикин.
2. Каждый шаг строго обоснован — никаких "очевидно".
3. Не ошибайся в вычислениях. Перепроверь каждый шаг.
4. <pre> — для блочных формул (1-2 строки). <code> — для inline.
5. Без вступлений — сразу 📝.

Если в промпте даны ПОХОЖИЕ РЕШЁННЫЕ ЗАДАЧИ из учебников — используй их как образец стиля и метода.
Не копируй буквально, реши именно ту задачу что на фото."""


async def solve_with_claude_vision(
    image_b64: str,
    media_type: str,
    rag_context: str = "",
    user_hint: str = "",
    use_thinking: bool | None = None,
) -> str:
    """Решить задачу: Claude получает фото + RAG-контекст похожих задач.

    use_thinking:
        True  → extended thinking (budget 2500) — для доказательств/исследований
        False → без thinking — для типовых вычислений (быстрее, дешевле в 2-3x)
        None  → использовать дефолт из settings.claude_use_extended_thinking
    """
    client = get_client()

    user_blocks: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_b64,
            },
        },
    ]

    text_parts = ["На фото задача по высшей математике. Распознай её и реши пошагово в указанном HTML-формате."]

    if user_hint:
        safe_hint = html.escape(user_hint)
        text_parts.append(f"\nКонтекст от студента: {safe_hint}")

    if rag_context:
        text_parts.append(
            "\n━━━ ПОХОЖИЕ ЗАДАЧИ ИЗ УЧЕБНИКОВ (используй как референс стиля и метода) ━━━\n"
            + rag_context
            + "\n━━━ КОНЕЦ ━━━\n\n"
            "Реши именно ту задачу, что на фото."
        )

    user_blocks.append({"type": "text", "text": "\n".join(text_parts)})

    kwargs: dict = {
        "model": settings.claude_model,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_blocks}],
    }

    # Router thinking: явный параметр > дефолт из settings
    thinking_on = use_thinking if use_thinking is not None else settings.claude_use_extended_thinking
    if thinking_on:
        # Budget 2500 (снижено с 8000) — хватает на доказательства,
        # экономия ~60% на токенах thinking. Для простых задач — без thinking совсем.
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 2500}
        kwargs["max_tokens"] = 5000

    response = await client.messages.create(**kwargs)

    solution_text = ""
    for block in response.content:
        if block.type == "text":
            solution_text += block.text

    logger.info(
        f"Claude solved (thinking={thinking_on}): in={response.usage.input_tokens}, "
        f"out={response.usage.output_tokens}"
    )

    return solution_text.strip()
