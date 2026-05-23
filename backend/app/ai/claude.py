"""Claude Sonnet 4.6 через ProxyAPI — Vision + Reasoning + Extended Thinking.

Два публичных метода:
1. extract_condition_text — лёгкий OCR-pass: фото → текст условия (для эмбеддинга/retrieval)
2. solve_with_claude_vision — главный solver с extended thinking + RAG-контекстом
"""
import html
import json
import re

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

OCR_SYSTEM = """Ты — OCR-распознаватель задач по точным дисциплинам (высшая математика, теория вероятностей и статистика, дискретная математика).
На фото — задача (или несколько) из учебника/задачника на русском. Аккуратно распознавай спецобозначения: вероятности P(A), мат. ожидание M[X]/E[X], дисперсию D[X], комбинаторику C_n^k, A_n^k, n!, символы множеств и логики (∈, ⊆, ∪, ∩, ∀, ∃, →, ¬), графы и таблицы.

Верни СТРОГО JSON (без markdown-обёртки, без пояснений) вида:
{
  "condition": "<текст условия НУЖНОЙ задачи одним абзацем, формулы в $...$>",
  "task_ids": ["<номера ВСЕХ отдельных задач, что видишь на фото>"]
}

Правила для "condition":
- Если на фото ОДНА задача (даже с подзадачами а/б/в) — распознай её полностью.
- Если НЕСКОЛЬКО задач и в подсказке студента указан номер ("задача 2851", "№3.14",
  "пример 5") — распознай ИМЕННО её. Если указана подзадача ("а)", "2851 в)") —
  распознай нужную подзадачу (с общей формулировкой задачи, если есть).
- Если несколько задач и подсказка пустая — распознай ПЕРВУЮ задачу.

Правила для "task_ids":
- Это номера ОТДЕЛЬНЫХ задач (например ["2851", "2852"]), а НЕ подпункты а/б/в.
- Если задача одна — верни список с одним номером (или [] если номера не видно).
- Перечисли только реально видимые на фото номера, по порядку."""


def _parse_ocr_json(raw: str) -> tuple[str, list[str]]:
    """Парсит JSON-ответ OCR. Устойчив к code-fence и мусору вокруг."""
    text = raw.strip()
    # снять возможную ```json ... ``` обёртку
    text = re.sub(r"^```(?:json)?\s*\n?|\n?```\s*$", "", text, flags=re.IGNORECASE).strip()
    # выдрать первый {...} блок если есть лишний текст
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        text = m.group(0)
    try:
        data = json.loads(text)
        condition = (data.get("condition") or "").strip()
        ids_raw = data.get("task_ids") or []
        # нормализуем: строки, без пустых, без дублей, сохраняя порядок
        seen = set()
        task_ids = []
        for x in ids_raw:
            s = str(x).strip()
            if s and s not in seen:
                seen.add(s)
                task_ids.append(s)
        return condition, task_ids
    except Exception:
        # не JSON — трактуем весь ответ как условие, номеров не знаем
        return raw.strip(), []


async def extract_condition_text(
    image_b64: str,
    media_type: str,
    user_hint: str = "",
) -> tuple[str, list[str]]:
    """Лёгкий vision-вызов: распознаёт условие нужной задачи + список номеров
    всех задач на фото.

    Возвращает (condition_text, task_ids). task_ids нужен, чтобы при нескольких
    задачах без подсказки спросить у юзера, какую решать.

    user_hint — что юзер написал в caption к фото (например "реши 2851 а)").
    """
    client = get_client()

    user_text = "Распознай условие и перечисли номера задач на фото. Верни JSON."
    if user_hint:
        user_text = (
            f"Студент написал: \"{user_hint}\".\n"
            f"С учётом этого распознай условие НУЖНОЙ задачи/подзадачи. "
            f"Также перечисли номера всех задач на фото. Верни JSON."
        )

    try:
        response = await client.messages.create(
            model=settings.ocr_model,
            max_tokens=700,
            system=OCR_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": image_b64,
                    }},
                    {"type": "text", "text": user_text},
                ],
            }],
        )
        raw = ""
        for block in response.content:
            if block.type == "text":
                raw += block.text
        condition, task_ids = _parse_ocr_json(raw)
        logger.info(
            f"OCR extract: in={response.usage.input_tokens}, "
            f"out={response.usage.output_tokens}, len={len(condition)}, task_ids={task_ids}"
        )
        return condition, task_ids
    except Exception as e:
        logger.warning(f"OCR extract failed: {e}")
    return "", []


# ───────────────────────────────────────────────────────────────────────
# 2) Главный solver — Claude Vision + extended thinking + RAG
# ───────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = r"""Ты — эксперт по высшей математике, теории вероятностей и математической статистике, дискретной математике; преподаватель технического вуза в РФ.
Студент 1-2 курса присылает фото задачи. Темы: матан, линейная алгебра, общая алгебра (группы, кольца, поля, многочлены), теория вероятностей и статистика, дискретная математика (графы, комбинаторика, теория множеств, математическая логика, рекуррентные соотношения). Дай ИДЕАЛЬНОЕ пошаговое решение.

═══════════════════════════════════════════════════════════════════════
ВЫБОР ЗАДАЧИ / ПОДЗАДАЧИ
═══════════════════════════════════════════════════════════════════════

На фото может быть ОДНА задача или НЕСКОЛЬКО (со своими номерами).
У задачи могут быть подзадачи: а), б), в), г) ...

ПРАВИЛА:
1. Если студент в "контекст от студента" написал номер задачи ("реши 2851",
   "пример 3.14", "№5") — решай ИМЕННО ЭТУ задачу с фото, остальные игнорируй.
2. Если указана подзадача ("а)", "пункт б", "2851 в)") — решай ТОЛЬКО эту подзадачу,
   игнорируй остальные пункты той же задачи.
3. Если контекст пустой — решай ПЕРВУЮ задачу на фото целиком (включая все её подзадачи если они есть).
4. Если несколько задач/подзадач и юзер не уточнил — НЕ решай все подряд. Возьми первую.
5. В заголовке "Условие" укажи номер и подзадачу которую решаешь (например: "Задача 2851 (а)").

═══════════════════════════════════════════════════════════════════════
ФОРМАТ ВЫВОДА — ЧИСТЫЙ LaTeX (будет скомпилирован в PNG картинку)
═══════════════════════════════════════════════════════════════════════

Решение будет вставлено в LaTeX-документ с уже подключёнными пакетами:
  amsmath, amssymb, amsthm, amsfonts, mathtools, babel(russian), T2A, xcolor.

Доступные пользовательские команды:
  \hd{Заголовок}   — синий заголовок-секция с подчёркиванием
  \ans{выражение}  — зелёная подпись "Ответ:" с выражением в рамке

ПРАВИЛА LaTeX:
- Inline формулы: $...$
- Блочные формулы: $$...$$ или \[...\] или displaymath
- Многострочные выкладки: \begin{align*} ... \end{align*}
- Системы: \begin{cases} ... \end{cases}
- Матрицы: \begin{pmatrix} ... \end{pmatrix}
- Текст на русском пишется обычно (без \text{})
- Внутри математики русский — \text{...}: $x \text{ при } y = 0$
- НЕ используй emoji в LaTeX, T2A их не поддерживает
- НЕ используй \section, \chapter, \part — у нас не такая структура

═══════════════════════════════════════════════════════════════════════
СТРУКТУРА ОТВЕТА — СТРОГО ВОТ ТАК
═══════════════════════════════════════════════════════════════════════

\hd{Условие}
Кратко переформулируй условие, формулы в \$...\$.

\hd{Что найти}
Чётко указать что искать: $\int f(x)\,dx$, $\det A$, и т.п.

\hd{Метод}
1-2 фразы о применяемой теореме / методе.

\hd{Решение}

\textbf{Шаг 1.} Пояснение почему делаем именно так.
$$ \text{формула с преобразованиями} $$

\textbf{Шаг 2.} Следующий шаг.
$$ \text{...} $$

\textit{(столько шагов сколько нужно — обычно 3-7)}

\hd{Ответ}
\ans{итоговое\_выражение}

═══════════════════════════════════════════════════════════════════════
ПРАВИЛА КАЧЕСТВА
═══════════════════════════════════════════════════════════════════════

1. Стиль — Демидович / Кудрявцев / Кострикин: строго, без воды.
2. Каждый шаг обоснован — никаких "очевидно".
3. НЕ ОШИБАЙСЯ В ВЫЧИСЛЕНИЯХ. Перепроверь каждый шаг.
4. Если есть ПОХОЖИЕ ЗАДАЧИ из учебников в контексте — используй их как образец стиля.
5. Не копируй RAG-примеры буквально — реши именно задачу на фото.
6. Выдавай ТОЛЬКО LaTeX-контент (без \documentclass, \begin{document}) — это будет вставлено в готовый шаблон.
7. НЕ оборачивай в ```latex ... ``` или ```math ... ``` — выдавай чистый LaTeX-код."""


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
