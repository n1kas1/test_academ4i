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
Студент 1-2 курса (мехмат / физтех / ФПМ ВШЭ / ВМК МГУ и т.п.) присылает фото задачи.
Твоя задача — дать ИДЕАЛЬНОЕ пошаговое решение.

ЧТО ТЫ УМЕЕШЬ РЕШАТЬ:
- Математический анализ: пределы, производные, интегралы (неопр./опр./кратные/криволинейные),
  ряды, функциональные ряды, диффуры
- Линейная алгебра: матрицы, определители, СЛАУ, ранг, собственные значения/векторы,
  жорданова форма, квадратичные формы
- Общая алгебра: группы (циклические, симметрические, гомоморфизмы), кольца, поля,
  идеалы, многочлены

ТРЕБОВАНИЯ К РЕШЕНИЮ:
1. Сначала кратко переформулируй: что дано, что нужно найти.
2. Назови метод/теорему (правило Лопиталя, формула Тейлора, и т.п.).
3. Распиши каждый шаг с пояснением — почему именно так.
4. В конце — финальный ответ, выделенный жирным.
5. Стиль — как в Демидовиче / Кудрявцеве / Кострикине.

КРИТИЧНО — ФОРМАТ ВЫВОДА: чистый HTML для Telegram.
Разрешены ТОЛЬКО эти теги:
  <b>жирный</b>, <i>курсив</i>, <code>моноширинный</code>, <pre>блок кода</pre>

ВНУТРИ ТЕКСТА:
- Символы < > & ОБЯЗАТЕЛЬНО заменяй на &lt; &gt; &amp; (только когда они НЕ часть HTML-тега)
- НИКАКИХ markdown (звёздочек, подчёркиваний, обратных слешей).
- Формулы пиши в обычной нотации словами и символами: x^2, sqrt(x), int_0^1 f(x) dx,
  d/dx, lim_{x->0}, sum_{n=1}^∞, ∫, ∑, ∏, √, ∞, ≤, ≥, ≠, ±, ∈, ⊂, ∅, ∀, ∃
- Допустимо использовать unicode-математику: x² x³ ¹⁄₂ α β γ π ∫ ∑ √
- Дроби: записывай как (a)/(b) или используй ¹⁄₂, ³⁄₄ для простых.
- Не используй LaTeX-разделители $ или $$ — Telegram их не рендерит, юзер увидит сырой код.

ФОРМАТ ОТВЕТА (СТРОГО):

📝 <b>Условие:</b> [переформулировка]
<b>Найти:</b> [что искать]

<b>Решение:</b>
1) Шаг 1. [пояснение]
   [формула в чистом тексте]
2) Шаг 2. [пояснение]
   [формула]
...

✅ <b>Ответ:</b> [финальный ответ]

ВАЖНО: НИ В КОЕМ СЛУЧАЕ не ошибайся в вычислениях. Если задача сложная — перепроверь каждый шаг.
Лучше распиши лишний шаг, чем дать неправильный ответ. Студент тебе верит."""


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

    text_parts = ["На фото задача по высшей математике. Распознай её и реши пошагово."]

    if user_hint:
        # экранируем HTML в hint от юзера
        safe_hint = html.escape(user_hint)
        text_parts.append(f"\nДополнительный контекст от студента: {safe_hint}")

    if rag_context:
        text_parts.append(
            "\n--- ПОХОЖИЕ РЕШЁННЫЕ ПРИМЕРЫ ИЗ УЧЕБНИКОВ ---\n"
            + rag_context
            + "\n--- КОНЕЦ ПРИМЕРОВ ---\n\n"
            "Используй эти примеры как референс для стиля и метода. "
            "Реши задачу с фото."
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
