"""LaTeX-санитайзер: детерминированный пост-процессинг сгенерированного LaTeX
ДО pdflatex. Цель — поймать типовые косяки модели до того, как они уронят
рендер и запустят дорогую цепочку DeepSeek-fix / DeepSeek-plain.

Покрывает 4 категории поломок, которые встречаются в логах:

1) Эмодзи — T2A их не знает, документ ломается.
   '🎯' → ''

2) Display-окружение внутри inline math.
   $\\begin{cases} ... \\end{cases}$  →  $$\\begin{cases} ... \\end{cases}$$
   Это самый частый кейс по логам.

3) Display-окружение внутри \\ans{...} (там \\boxed, он inline-only).
   \\ans{$\\begin{cases}...\\end{cases}$}
     → $$\\begin{cases}...\\end{cases}$$
       \\ans{\\text{см. формулу выше}}

4) Кириллица в math без \\text{}.
   $x при y = 0$  →  $x \\text{при} y = 0$
   Покрывает $...$, $$...$$, \\(...\\), \\[...\\] и block-окружения.

Санитайзер ИДЕМПОТЕНТЕН: повторный прогон не меняет результат.
"""
import re


# ─────────────────── 1. Эмодзи ────────────────────────────────────────

# Основные диапазоны emoji/пиктограмм/стрелок Unicode.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"    # MISC SYMBOLS AND PICTOGRAPHS, EMOTICONS, etc.
    "\U00002600-\U000027BF"    # MISC SYMBOLS, DINGBATS
    "\U0001F000-\U0001F02F"    # MAHJONG, DOMINO
    "\U00002B00-\U00002BFF"    # ARROWS
    "\U0000FE0F"                # VARIATION SELECTOR-16 (хвост эмодзи)
    "]+"
)


def strip_emoji(text: str) -> str:
    """Удалить эмодзи (для T2A-совместимости)."""
    return _EMOJI_RE.sub("", text)


# ─────────────────── 2. Block-env внутри inline $...$ ─────────────────

# Перечисление "блочных" math-окружений — они требуют display math.
_BLOCK_ENVS = (
    "cases", "align", "align*", "aligned", "gather", "gather*",
    "matrix", "pmatrix", "bmatrix", "vmatrix", "Bmatrix", "Vmatrix",
    "equation", "equation*", "multline", "multline*", "displaymath", "split",
)
_BLOCK_ENVS_ALT = "|".join(re.escape(e) for e in _BLOCK_ENVS)

# $<...>\begin{block}...\end{block}<...>$  →  $$\1$$
_INLINE_WITH_BLOCK_RE = re.compile(
    r"(?<!\\)(?<!\$)\$(?!\$)"
    r"((?:[^$]|\\\$)*?\\begin\{(?:" + _BLOCK_ENVS_ALT + r")\}.*?\\end\{[^{}]+\}(?:[^$]|\\\$)*?)"
    r"(?<!\\)(?<!\$)\$(?!\$)",
    re.DOTALL,
)


def fix_block_in_inline(text: str) -> str:
    """$...\\begin{cases}...\\end{cases}...$  →  $$...$$"""
    return _INLINE_WITH_BLOCK_RE.sub(r"$$\1$$", text)


# ─────────────────── 3. Block-env внутри \ans{...} ────────────────────

_ANS_NEEDLE = r"\ans{"


def _find_ans_spans(text: str) -> list[tuple[int, int, str]]:
    """Найти все \\ans{...} с ПРОИЗВОЛЬНОЙ вложенностью {}.

    Возвращает [(start, end, body)] где start — позиция '\\ans', end — позиция
    сразу за закрывающей '}'. Использует балансовый счётчик скобок, поэтому
    корректно работает с \\ans{\\dfrac{x^{2}+1}{x-1}} и глубже.
    """
    spans: list[tuple[int, int, str]] = []
    i = 0
    n = len(text)
    while i < n:
        j = text.find(_ANS_NEEDLE, i)
        if j == -1:
            return spans
        k = j + len(_ANS_NEEDLE)
        depth = 1
        while k < n and depth > 0:
            c = text[k]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            k += 1
        if depth != 0:
            # Незакрытая \ans — пропускаем (битый LaTeX, не наш косяк).
            return spans
        spans.append((j, k, text[j + len(_ANS_NEEDLE):k - 1]))
        i = k
    return spans


def fix_ans_with_block(text: str) -> str:
    """\\ans{ ... \\begin{cases}/align*/... } → выносим как $$...$$, оставляем
    \\ans{\\text{см. формулу выше}} ради сохранения структуры шаблона.
    """
    spans = _find_ans_spans(text)
    if not spans:
        return text
    block_re = re.compile(r"\\begin\{(?:" + _BLOCK_ENVS_ALT + r")\}")
    out: list[str] = []
    last = 0
    for start, end, body in spans:
        out.append(text[last:start])
        body_stripped = body.strip()
        if not block_re.search(body_stripped):
            # Безобидный \ans — оставляем как есть.
            out.append(text[start:end])
        else:
            s = body_stripped
            # Снимаем внешние $$...$$ или $...$ ради чистоты блока.
            if s.startswith("$$") and s.endswith("$$") and len(s) >= 4:
                s = s[2:-2].strip()
            elif s.startswith("$") and s.endswith("$") and len(s) >= 2:
                s = s[1:-1].strip()
            out.append(f"$$\n{s}\n$$\n\\ans{{\\text{{см. формулу выше}}}}")
        last = end
    out.append(text[last:])
    return "".join(out)


# ─────────────────── 4. Кириллица в math без \text{} ──────────────────

# Слово из 2+ кириллических букв (одиночные буквы — оставляем, могут быть переменными).
# Соседние слова через пробел/дефис включаем в одну группу.
_CYR_WORD_RE = re.compile(r"[а-яА-ЯёЁ]{2,}(?:[ \-][а-яА-ЯёЁ]+)*")

# Команды-обёртки, которые уже корректно содержат текст в math-режиме.
_TEXT_LIKE_CMDS = (
    "text", "textbf", "textit", "textsf", "texttt",
    "mathrm", "mathit", "mathbf", "mathsf", "mathtt",
    "mbox", "operatorname", "operatorname*",
)


_MAX_STASH_PASSES = 8  # глубина вложенности \text{\textbf{...}} безопасный потолок.


def _stash_text(s: str) -> tuple[str, list[str]]:
    """Прячем \\text{...} и подобные за \x00P{i}\x00, чтобы не оборачивать дважды.

    Multi-pass: каждый проход прячет внешний "лист" (\\text{} без внутр. {}). После
    замены вложенные \\text-команды становятся листами для следующего прохода.
    Это покрывает кейсы вроде \\text{слово \\textbf{другое}}.
    """
    placeholders: list[str] = []

    def stash(match: "re.Match[str]") -> str:
        placeholders.append(match.group(0))
        return f"\x00P{len(placeholders) - 1}\x00"

    for _ in range(_MAX_STASH_PASSES):
        changed = False
        for cmd in _TEXT_LIKE_CMDS:
            new_s = re.sub(rf"\\{re.escape(cmd)}\{{[^{{}}]*\}}", stash, s)
            if new_s != s:
                changed = True
                s = new_s
        if not changed:
            break
    return s, placeholders


def _restore_text(s: str, placeholders: list[str]) -> str:
    for i, original in enumerate(placeholders):
        s = s.replace(f"\x00P{i}\x00", original)
    return s


def _wrap_cyr_in_block(content: str) -> str:
    """Внутри одного math-блока: оборачиваем кириллицу в \\text{...}."""
    stashed, placeholders = _stash_text(content)
    stashed = _CYR_WORD_RE.sub(lambda m: f"\\text{{{m.group(0).strip()}}}", stashed)
    return _restore_text(stashed, placeholders)


# Math-сегменты, по которым итерируемся. Порядок важен: сначала $$, потом $.
_DISPLAY_DOLLAR_RE = re.compile(r"(\$\$)((?:[^$]|\\\$)+?)(\$\$)", re.DOTALL)
_DISPLAY_BRACKET_RE = re.compile(r"(\\\[)(.+?)(\\\])", re.DOTALL)
_INLINE_PAREN_RE = re.compile(r"(\\\()(.+?)(\\\))", re.DOTALL)
_INLINE_DOLLAR_RE = re.compile(
    r"((?<!\\)(?<!\$)\$(?!\$))((?:[^$\n]|\\\$)+?)((?<!\\)(?<!\$)\$(?!\$))"
)
_BLOCK_ENV_RE = re.compile(
    r"(\\begin\{(" + _BLOCK_ENVS_ALT + r")\})(.+?)(\\end\{\2\})", re.DOTALL
)


def wrap_cyrillic_in_math(text: str) -> str:
    """Оборачивает кириллические слова в \\text{} ВНУТРИ math-режимов."""
    def repl_simple(m: "re.Match[str]") -> str:
        return m.group(1) + _wrap_cyr_in_block(m.group(2)) + m.group(3)

    def repl_env(m: "re.Match[str]") -> str:
        # m.group(1) = \begin{env}, m.group(2) = env name, m.group(3) = body, m.group(4) = \end{env}
        return m.group(1) + _wrap_cyr_in_block(m.group(3)) + m.group(4)

    text = _DISPLAY_DOLLAR_RE.sub(repl_simple, text)
    text = _DISPLAY_BRACKET_RE.sub(repl_simple, text)
    text = _INLINE_PAREN_RE.sub(repl_simple, text)
    text = _INLINE_DOLLAR_RE.sub(repl_simple, text)
    text = _BLOCK_ENV_RE.sub(repl_env, text)
    return text


# ─────────────────── Детектор проблем (для инструментации) ────────────

# Литеральные Unicode-символы, которые под T2A фатальны без маппинга в шаблоне
# (греческий блок, математические операторы/стрелки/буквоподобные, доп. операторы).
# Шаблон (latex_to_png.LATEX_TEMPLATE) их перехватывает через \newunicodechar —
# но если символ всё-таки не в карте, это сигнал для разбора падений.
_LITERAL_UNICODE_RE = re.compile(
    "["
    "Ͱ-Ͽ"    # Greek
    "⁰-₟"    # super/subscripts
    "℀-⅏"    # letterlike (ℝ ℕ …)
    "←-⇿"    # arrows
    "∀-⋿"    # math operators
    "⨀-⫿"    # supplemental math operators
    "±×÷°"  # ± × ÷ °
    "]"
)


def detect_latex_issues(latex: str) -> list[str]:
    """Эвристически метит потенциально фатальные проблемы LaTeX. НЕ мутирует —
    только для инструментации (таблица render_failures), чтобы видеть РЕАЛЬНОЕ
    распределение причин падений, а не гадать.

    Теги: odd_dollars / brace_mismatch / literal_unicode / emoji.
    """
    if not latex:
        return []
    issues: list[str] = []
    # Нечётное число одиночных $ (после снятия экранированных \$ и парных $$).
    tmp = latex.replace(r"\$", "").replace("$$", "")
    if tmp.count("$") % 2 == 1:
        issues.append("odd_dollars")
    # Дисбаланс {} (игнорируя экранированные \{ \}).
    no_esc = re.sub(r"\\[{}]", "", latex)
    if no_esc.count("{") != no_esc.count("}"):
        issues.append("brace_mismatch")
    if _LITERAL_UNICODE_RE.search(latex):
        issues.append("literal_unicode")
    if _EMOJI_RE.search(latex):
        issues.append("emoji")
    return issues


# ─────────────────── Главная функция ──────────────────────────────────


def sanitize_for_render(latex: str) -> str:
    """Все детерминированные фиксы, в правильном порядке. Идемпотентна.

    Примечание: маппинг литерального Unicode (≤ ∫ α …) делается НЕ здесь, а в
    LaTeX-шаблоне через \\newunicodechar — так надёжнее: покрывает символы в любом
    контексте, включая \\text{...}, без хрупких строковых замен.
    """
    if not latex:
        return latex
    out = latex
    out = strip_emoji(out)
    out = fix_block_in_inline(out)
    out = fix_ans_with_block(out)
    out = wrap_cyrillic_in_math(out)
    return out
