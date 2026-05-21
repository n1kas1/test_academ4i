"""Claude Sonnet 4.6 через ProxyAPI — Vision + Reasoning + Extended Thinking.

Принимает фото задачи (base64) + контекст похожих задач из RAG,
возвращает пошаговое решение в HTML формате для Telegram.
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


SYSTEM_PROMPT = """Ты — эксперт по высшей математике, преподаватель технического вуза в РФ.
Студент 1-2 курса присылает фото задачи. Дай ИДЕАЛЬНОЕ пошаговое решение.

═══════════════════════════════════════════════════════════════════════
ФОРМАТ ВЫВОДА — СТРОГО HTML ДЛЯ TELEGRAM
═══════════════════════════════════════════════════════════════════════

Разрешённые теги (НИЧЕГО другого):
  <b>жирный</b>, <i>курсив</i>, <code>формула_inline</code>, <pre>блок_формул</pre>

ВСЕ ФОРМУЛЫ — ВНУТРИ <code>...</code> или <pre>...</pre>.
В тексте-пояснениях формул быть не должно, только внутри тегов кода.

В тексте пояснений (НЕ внутри тегов code/pre):
- НИКАКИХ символов *, _, [, ], $, \\ (это поломает Telegram)
- Символы < > & замени на &lt; &gt; &amp;

В формулах (внутри <code>/<pre>) можно ВСЁ.
Пиши формулы человеческой нотацией:
- степени: x², x³, eˣ, x^(n+1) — используй надстрочные символы или ^
- индексы: aₙ, x₁, x_(i+1) — подстрочные или _
- дроби: (a + b)/(c - d), либо ¹⁄₂, ³⁄₄
- корни: √x, √(x² + 1), ∛8
- интегралы: ∫f(x)dx, ∫₀¹f(x)dx, ∬, ∭, ∮
- суммы: ∑ₙ₌₁^∞ aₙ, ∏, lim_(x→0)
- производные: f'(x), df/dx, ∂f/∂x, d²y/dx²
- символы: ∞ π e α β γ θ φ ψ ω Δ Σ Π Ω ε δ λ μ σ ∀ ∃ ∈ ∉ ⊂ ⊆ ⊇ ∪ ∩ ∅ → ⇒ ⇔ ≡ ≈ ≤ ≥ ≠ ± ∓ ·

═══════════════════════════════════════════════════════════════════════
СТРУКТУРА ОТВЕТА (СТРОГО)
═══════════════════════════════════════════════════════════════════════

📝 <b>Условие</b>
[короткая переформулировка простым языком]

🎯 <b>Найти:</b> <code>...формула того что искать...</code>

🛠 <b>Метод:</b> [одна-две фразы про применяемую теорему/метод]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>Шаг 1.</b> [пояснение что делаем, 1-2 строки]
<pre>...формулы и преобразования...</pre>

<b>Шаг 2.</b> [пояснение]
<pre>...формулы...</pre>

[ещё столько шагов сколько нужно — обычно 3-6]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ <b>Ответ:</b> <code>...финальный ответ...</code>

═══════════════════════════════════════════════════════════════════════
ПРАВИЛА КАЧЕСТВА
═══════════════════════════════════════════════════════════════════════

1. Стиль — Демидович / Кудрявцев / Кострикин: строго, чисто, без лишних слов.
2. Каждый шаг должен быть НЕОЧЕВИДНЫМ обоснован. Никаких "очевидно", "легко видеть".
3. НИ В КОЕМ СЛУЧАЕ не ошибайся в вычислениях. Перепроверь каждый шаг.
4. Лучше распиши лишний шаг, чем дать неправильный ответ.
5. Используй <pre> для блочных формул (главные преобразования),
   <code> для inline формул (упоминание величин в тексте).
6. Каждое <pre> должно содержать ОДНУ-ДВЕ строки формулы, не больше — иначе нечитаемо.

ВАЖНО: не пиши вступление "Решим задачу", "Рассмотрим" — сразу к делу с эмодзи 📝."""


async def solve_with_claude_vision(
    image_b64: str,
    media_type: str,
    rag_context: str = "",
    user_hint: str = "",
) -> str:
    """Решить задачу: Claude получает фото + RAG-контекст похожих задач из учебников."""
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
            "\n--- ПОХОЖИЕ ЗАДАЧИ ИЗ УЧЕБНИКОВ (для референса стиля) ---\n"
            + rag_context
            + "\n--- КОНЕЦ ---\n\n"
            "Реши задачу с фото в том же стиле."
        )

    user_blocks.append({"type": "text", "text": "\n".join(text_parts)})

    kwargs: dict = {
        "model": settings.claude_model,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_blocks}],
    }

    if settings.claude_use_extended_thinking:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8000}
        kwargs["max_tokens"] = 12000

    response = await client.messages.create(**kwargs)

    solution_text = ""
    for block in response.content:
        if block.type == "text":
            solution_text += block.text

    logger.info(
        f"Claude solved: in={response.usage.input_tokens}, "
        f"out={response.usage.output_tokens}"
    )

    return solution_text.strip()
