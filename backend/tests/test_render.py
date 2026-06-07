"""Тесты надёжности рендера и графики (Workstream B + A).

Покрывают чистые/детерминированные части без реального pdflatex:
  • detect_latex_issues — категоризация причин падений (для инструментации);
  • render_figures_in_latex — подстановка/удаление %%FIG-блоков (compile_figure мокается);
  • FIG_RE — контракт протокола рисунков;
  • classify_topic + sanitize на реальном падавшем кейсе пользователя (классы Поста).

Реальная компиляция (pdflatex/TikZ) тут НЕ запускается — проверяется в Docker
вручную (см. план, раздел «Верификация»).
"""
import asyncio
import pathlib

from app.ai.latex_sanitize import detect_latex_issues, sanitize_for_render
from app.ai.pipeline import classify_topic
from app.render import figures


# ─────────────────── detect_latex_issues ──────────────────────────────

class TestDetectLatexIssues:
    def test_clean_latex_no_issues(self):
        assert detect_latex_issues(r"\hd{Решение} $x^2 + 1$ \ans{2}") == []

    def test_odd_dollars(self):
        assert "odd_dollars" in detect_latex_issues(r"формула $x^2 + 1 без закрытия")

    def test_even_dollars_ok(self):
        assert "odd_dollars" not in detect_latex_issues(r"$a$ и $b$")

    def test_display_dollars_not_counted_as_odd(self):
        # $$...$$ — чётные пары, не должны метиться как odd_dollars.
        assert "odd_dollars" not in detect_latex_issues(r"$$x^2$$")

    def test_escaped_dollar_ignored(self):
        assert "odd_dollars" not in detect_latex_issues(r"цена \$5 за штуку")

    def test_brace_mismatch(self):
        assert "brace_mismatch" in detect_latex_issues(r"\frac{x}{y")

    def test_balanced_braces_ok(self):
        assert "brace_mismatch" not in detect_latex_issues(r"\frac{x}{y}")

    def test_literal_unicode_flagged(self):
        # ≤ ∫ α — литеральный Unicode, под T2A фатален без \newunicodechar.
        issues = detect_latex_issues("$x ≤ 5$ интеграл ∫ и α")
        assert "literal_unicode" in issues

    def test_emoji_flagged(self):
        assert "emoji" in detect_latex_issues("Ответ 🎯 готов")

    def test_empty_input(self):
        assert detect_latex_issues("") == []


# ─────────────────── FIG_RE / render_figures_in_latex ──────────────────

_FIG_LATEX = (
    r"\hd{Решение}" "\n"
    "Смотри график:\n"
    "%%FIG\n"
    r"\begin{tikzpicture}\draw (0,0)--(1,1);\end{tikzpicture}" "\n"
    "%%ENDFIG\n"
    r"\ans{x}"
)


class TestFigRegex:
    def test_extracts_block(self):
        m = figures.FIG_RE.search(_FIG_LATEX)
        assert m is not None
        assert "tikzpicture" in m.group(1)

    def test_no_block_no_match(self):
        assert figures.FIG_RE.search(r"\hd{Решение} $x$") is None

    def test_multiple_blocks(self):
        двойной = _FIG_LATEX + "\n%%FIG\n\\node{A};\n%%ENDFIG"
        assert len(figures.FIG_RE.findall(двойной)) == 2


class TestRenderFiguresInLatex:
    def test_noop_without_blocks(self):
        latex = r"\hd{Решение} $x^2$ \ans{2}"
        out, ok, failed = asyncio.run(figures.render_figures_in_latex(latex))
        assert out == latex and ok == 0 and failed == 0

    def test_substitutes_on_success(self, monkeypatch):
        async def fake_ok(_body):
            return pathlib.Path("/app/render_cache/figures/deadbeef.png")
        monkeypatch.setattr(figures, "compile_figure", fake_ok)
        out, ok, failed = asyncio.run(figures.render_figures_in_latex(_FIG_LATEX))
        assert ok == 1 and failed == 0
        assert "%%FIG" not in out and "%%ENDFIG" not in out
        assert r"\includegraphics" in out
        assert "deadbeef.png" in out

    def test_drops_block_on_failure(self, monkeypatch):
        async def fake_fail(_body):
            return None
        monkeypatch.setattr(figures, "compile_figure", fake_fail)
        out, ok, failed = asyncio.run(figures.render_figures_in_latex(_FIG_LATEX))
        assert ok == 0 and failed == 1
        assert "%%FIG" not in out and "tikzpicture" not in out
        # Само решение (вне блока) уцелело.
        assert r"\hd{Решение}" in out and r"\ans{x}" in out

    def test_sanitize_strips_caption(self):
        # Реальная причина падений: модель добавляет \caption → в standalone фатально.
        body = r"\begin{tikzpicture}\node{A};\end{tikzpicture}\caption{$A_2$}"
        out = figures._sanitize_figure_body(body)
        assert "caption" not in out and "tikzpicture" in out

    def test_sanitize_strips_figure_wrappers(self):
        body = (r"\begin{figure}[h]\centering"
                r"\begin{tikzpicture}\draw(0,0)--(1,1);\end{tikzpicture}"
                r"\caption{x}\label{fig:1}\end{figure}")
        out = figures._sanitize_figure_body(body)
        for bad in (r"\begin{figure}", r"\end{figure}", r"\centering", r"\caption", r"\label"):
            assert bad not in out
        assert r"\begin{tikzpicture}" in out and r"\draw(0,0)--(1,1)" in out

    def test_sanitize_keeps_clean_body(self):
        body = r"\begin{tikzpicture}\draw(0,0) circle(1);\end{tikzpicture}"
        assert figures._sanitize_figure_body(body) == body

    def test_sanitize_does_not_eat_similar_command(self):
        # \drawlabel не должен ловиться как \label (нет '\' прямо перед 'label').
        assert figures._sanitize_figure_body(r"\drawlabel{x}") == r"\drawlabel{x}"

    def test_mixed_success_and_failure(self, monkeypatch):
        calls = {"n": 0}

        async def fake(_body):
            calls["n"] += 1
            return pathlib.Path("/x/a.png") if calls["n"] == 1 else None

        monkeypatch.setattr(figures, "compile_figure", fake)
        latex = _FIG_LATEX + "\n%%FIG\n\\bad tikz\n%%ENDFIG"
        out, ok, failed = asyncio.run(figures.render_figures_in_latex(latex))
        assert ok == 1 and failed == 1
        assert out.count(r"\includegraphics") == 1


# ─────────────────── Кириллица-индекс в math (root cause 88 ошибок) ────

class TestCyrillicSubscriptInMath:
    def test_single_cyrillic_subscript_wrapped(self):
        # $\rho_ш V_ж$ — ш/ж как индексы по-русски (шар/жидкость). Должны уйти в \text{}.
        out = sanitize_for_render(r"$\rho_ш V_ж$")
        assert r"\text{ш}" in out and r"\text{ж}" in out

    def test_no_bare_cyrillic_in_math_after_sanitize(self):
        from app.ai.pipeline import _has_cyrillic_in_math
        out = sanitize_for_render(r"$\rho_ш V_ж = m_т g$ обычный текст")
        assert not _has_cyrillic_in_math(out)

    def test_idempotent(self):
        once = sanitize_for_render(r"масса $\rho_ш$ и $$m_т = \frac{4}{3}\pi r^3 \rho_ш$$")
        assert sanitize_for_render(once) == once

    def test_plain_text_cyrillic_untouched(self):
        # Вне math кириллица не трогается.
        assert sanitize_for_render("Просто русский текст без формул") == "Просто русский текст без формул"


# ─────────────────── Принудительный рисунок по просьбе юзера ───────────

class TestUserWantsFigure:
    def test_draw_verbs_trigger(self):
        from app.ai.pipeline import _user_wants_figure
        assert _user_wants_figure("", "нарисуй графики распределения и плотности")
        assert _user_wants_figure("постройте график функции y=x^2", "")
        assert _user_wants_figure("", "изобрази схему сил")

    def test_no_trigger_on_plain_task(self):
        from app.ai.pipeline import _user_wants_figure
        assert not _user_wants_figure("найти производную функции", "реши пункт б")
        assert not _user_wants_figure("вычислить интеграл", "")


class TestExtractFigBlock:
    def test_extracts_marked_block(self):
        from app.ai.deepseek import _extract_fig_block
        raw = "текст\n%%FIG\n\\begin{tikzpicture}\\draw(0,0)--(1,1);\\end{tikzpicture}\n%%ENDFIG\nещё"
        out = _extract_fig_block(raw)
        assert out.startswith("%%FIG") and out.rstrip().endswith("%%ENDFIG")
        assert "tikzpicture" in out

    def test_wraps_bare_tikzpicture(self):
        from app.ai.deepseek import _extract_fig_block
        raw = "```latex\n\\begin{tikzpicture}\\draw(0,0)circle(1);\\end{tikzpicture}\n```"
        out = _extract_fig_block(raw)
        assert "%%FIG" in out and "tikzpicture" in out and "```" not in out

    def test_empty_when_no_figure(self):
        from app.ai.deepseek import _extract_fig_block
        assert _extract_fig_block("просто текст без рисунка") == ""


# ─────────────────── Реальный кейс пользователя (классы Поста) ─────────

# Условие, которое падало у пользователя (дискретка: минимальные полные
# подсистемы булевых функций + функциональные схемы).
_POST_CASE = (
    "Перечислите все минимальные полные подсистемы системы {f1, f2, f3, f4, f5, f6}. "
    "Для одной из них выразите функции 0, 1, ¬, ∨, ∧, ⊕, штрих Шеффера и постройте "
    "их функциональные схемы. Таблица (вектора значений): f1: 1110 0111; f2: 0001 0111"
)


class TestPostClassesCase:
    def test_classified_as_discrete(self):
        # Раньше уходило в matan/lin_alg → терялся RAG из дискретки.
        assert classify_topic(_POST_CASE) == "discrete"

    def test_sanitize_idempotent_on_case(self):
        once = sanitize_for_render(_POST_CASE)
        twice = sanitize_for_render(once)
        assert once == twice

    def test_literal_unicode_detected(self):
        # В условии есть ¬ ∨ ∧ ⊕ — детектор должен их пометить (шаблон их мапит).
        assert "literal_unicode" in detect_latex_issues(_POST_CASE)
