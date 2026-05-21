"""Claude 3.7 Sonnet через ProxyAPI — Vision + Reasoning + Extended Thinking.

Главная функция: принимает фото задачи (base64) и контекст похожих задач из RAG,
возвращает пошаговое решение в формате MarkdownV2 для Telegram.
"""
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
Студент 1-2 курса (мехмат / физтех / ФПМ ВШЭ / ВМК МГУ и т.п.) присылает тебе фото
задачи. Твоя задача — дать ИДЕАЛЬНОЕ пошаговое решение.

ЧТО ТЫ УМЕЕШЬ РЕШАТЬ:
- Математический анализ: пределы, производные, интегралы (неопр./опр./кратные/криволинейные),
  ряды, функциональные ряды, диффуры, метрические/нормированные пространства
- Линейная алгебра: матрицы, определители, СЛАУ, ранг, собственные значения/векторы,
  жорданова форма, квадратичные формы, евклидовы/унитарные пространства
- Общая алгебра: группы (циклические, симметрические, гомоморфизмы), кольца, поля,
  идеалы, многочлены, теория Галуа

ТРЕБОВАНИЯ К РЕШЕНИЮ:
1. Сначала переформулируй условие своими словами: что дано, что нужно найти/доказать.
2. Если задача нестандартная — кратко назови метод и теорему (например, "правило Лопиталя",
   "теорема о среднем", "формула Тейлора с остаточным членом в форме Лагранжа").
3. Распиши каждый шаг с пояснением — почему именно так. НЕ "очевидно", НЕ "легко видеть".
4. Все формулы — в LaTeX внутри $...$ для inline или $$...$$ для блочных.
5. В конце — финальный ответ ВЫДЕЛЕННЫЙ.
6. Стиль — как в Демидовиче / Кудрявцеве / Кострикине.

ФОРМАТ ВЫВОДА (MarkdownV2 для Telegram, ОБЯЗАТЕЛЬНО экранируй спец-символы
. - ! ( ) | { } # + = > _ * [ ] ~ ` обратным слешем, КРОМЕ внутри $..$ формул):

📝 *Условие:* ...
*Найти:* ...

*Решение:*
1\\) Шаг 1: ... $формула$
2\\) Шаг 2: ...
...

✅ *Ответ:* ...

КРИТИЧНО: НЕ ОШИБАЙСЯ В ВЫЧИСЛЕНИЯХ. Если задача сложная — используй extended thinking
для перепроверки шагов. Лучше потратить больше токенов, чем дать неправильный ответ.
Студент тебе верит."""


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
        text_parts.append(f"\nДополнительный контекст от студента: {user_hint}")

    if rag_context:
        text_parts.append(
            "\n--- ПОХОЖИЕ РЕШЁННЫЕ ПРИМЕРЫ ИЗ КЛАССИЧЕСКИХ ЗАДАЧНИКОВ ---\n"
            + rag_context
            + "\n--- КОНЕЦ ПРИМЕРОВ ---\n\n"
            "Используй эти примеры как референс для стиля, метода и оформления."
            " Реши именно задачу на фото (не из примеров)."
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
        # При extended thinking max_tokens должен быть > budget_tokens
        kwargs["max_tokens"] = 12000

    response = await client.messages.create(**kwargs)

    solution_text = ""
    for block in response.content:
        if block.type == "text":
            solution_text += block.text

    logger.info(
        f"Claude solved: in={response.usage.input_tokens}, "
        f"out={response.usage.output_tokens}, "
        f"thinking={getattr(response.usage, 'cache_creation_input_tokens', 0)}"
    )

    return solution_text.strip()
