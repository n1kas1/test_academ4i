"""TikZ/pgfplots-рисунки → PNG, изолированная компиляция.

Pipeline для каждого рисунка:
  %%FIG\\n<tikz body>\\n%%ENDFIG → standalone pdflatex → pdftoppm → trim → PNG.

Ошибка в отдельном рисунке НЕ роняет всё решение — блок молча удаляется.
Вставка в основной документ: \\includegraphics с абсолютным путём в кэше.

Dockerfile-зависимости: те же что и для latex_to_png — texlive-* + poppler-utils.
"""
import asyncio
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from app.ai.latex_sanitize import strip_dangerous_tex
from app.render.latex_to_png import CACHE_DIR, PREVIEW_DPI, _TEX_SAFE_ENV, _trim_white

# ── Кэш-директория для рисунков (персистентна в контейнере) ─────────────────
FIGURE_CACHE_DIR = CACHE_DIR / "figures"
FIGURE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Инкрементировать при изменении STANDALONE_TEMPLATE — инвалидирует кэш.
FIGURE_TEMPLATE_VERSION = "fig_v3"

# DoS-защита: максимум рисунков на одно решение (лишние молча отбрасываем) и
# глобальный потолок параллельных компиляций (каждая = отдельный pdflatex-процесс).
_MAX_FIGURES_PER_DOC = 6
_FIG_COMPILE_SEM = asyncio.Semaphore(3)

# Регексп для маркеров, которые расставляет модель вокруг TikZ-кода.
FIG_RE = re.compile(r"%%FIG\s*(.*?)%%ENDFIG", re.DOTALL)

# Standalone-документ: border=4pt обрезает поля по контенту автоматически.
STANDALONE_TEMPLATE = r"""\documentclass[border=4pt]{standalone}
\usepackage[utf8]{inputenc}
\usepackage[T2A]{fontenc}
\usepackage[russian]{babel}
\usepackage{amsmath,amssymb}
\usepackage{tikz}
\usepackage{circuitikz}
\usepackage{pgfplots}
\pgfplotsset{compat=1.18}
\usepgfplotslibrary{fillbetween}
\usetikzlibrary{arrows.meta,positioning,calc,patterns,shapes.geometric,shapes.misc,circuits.logic.IEC,automata,decorations.pathmorphing,intersections,angles,quotes,through,backgrounds}
\begin{document}
%CONTENT%
\end{document}
"""


# ── Очистка тела рисунка от float-обёрток ────────────────────────────────────
# Модель часто оборачивает TikZ в \begin{figure}/\caption/\label/\centering.
# В standalone-документе \caption ВНЕ float → фатальная ошибка → нет PDF.
# Срезаем эти обёртки детерминированно (по логам — реальная причина падений).

def _remove_cmd_with_arg(text: str, cmd: str) -> str:
    r"""Удалить все вхождения \cmd[..]{..} с балансировкой скобок (\caption, \label)."""
    needle = "\\" + cmd
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        j = text.find(needle, i)
        if j == -1:
            out.append(text[i:])
            break
        # не цепляем более длинные команды (\labelfoo) — следующий символ не буква
        nxt = j + len(needle)
        if nxt < n and text[nxt].isalpha():
            out.append(text[i:nxt])
            i = nxt
            continue
        out.append(text[i:j])
        k = nxt
        if k < n and text[k] == "*":
            k += 1
        while k < n and text[k] in " \t":
            k += 1
        if k < n and text[k] == "[":  # опциональный [..]
            depth, k = 1, k + 1
            while k < n and depth:
                depth += (text[k] == "[") - (text[k] == "]")
                k += 1
        while k < n and text[k] in " \t\n":
            k += 1
        if k < n and text[k] == "{":  # обязательный {..}
            depth, k = 1, k + 1
            while k < n and depth:
                depth += (text[k] == "{") - (text[k] == "}")
                k += 1
        i = k
    return "".join(out)


def _sanitize_figure_body(body: str) -> str:
    r"""Снять float-обёртки + опасные TeX-примитивы (\input/\write/...) из LLM-TikZ."""
    body = strip_dangerous_tex(body)  # безопасность: LFI/IO в теле рисунка
    body = re.sub(r"\\begin\{figure\*?\}(\[[^\]]*\])?", "", body)
    body = re.sub(r"\\end\{figure\*?\}", "", body)
    body = re.sub(r"\\centering\b", "", body)
    body = _remove_cmd_with_arg(body, "caption")
    body = _remove_cmd_with_arg(body, "label")
    return body.strip()


# ── Хэш ─────────────────────────────────────────────────────────────────────

def _figure_hash(tikz_body: str) -> str:
    """Хэш зависит от версии шаблона — изменение STANDALONE_TEMPLATE инвалидирует кэш."""
    key = f"{FIGURE_TEMPLATE_VERSION}:{tikz_body}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# ── Синхронная компиляция ────────────────────────────────────────────────────

def _compile_figure_sync(tikz_body: str, out_png: Path) -> bool:
    """Синхронно компилирует TikZ-тело в PNG.

    Args:
        tikz_body: Сырой TikZ-код (тело между %%FIG / %%ENDFIG).
        out_png: Путь для записи результирующего PNG.

    Returns:
        True при успехе, False при любой ошибке.
        Исключения НЕ пробрасываются наружу — только лог.
    """
    with tempfile.TemporaryDirectory(prefix="tikz_") as tmpdir:
        tmp = Path(tmpdir)
        tex_path = tmp / "fig.tex"
        full_doc = STANDALONE_TEMPLATE.replace("%CONTENT%", tikz_body)
        tex_path.write_text(full_doc, encoding="utf-8")

        # 1) pdflatex с halt-on-error: нам нужен либо валидный рисунок, либо ничего.
        #    -no-shell-escape: контент от LLM — отключаем \write18.
        try:
            res = subprocess.run(
                [
                    "pdflatex",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-no-shell-escape",
                    "-output-directory", str(tmp),
                    str(tex_path),
                ],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=_TEX_SAFE_ENV,  # openin_any=p — запрет чтения произвольных файлов
            )
        except subprocess.TimeoutExpired:
            logger.error("figures: pdflatex timeout")
            return False

        pdf_path = tmp / "fig.pdf"
        if not pdf_path.exists():
            tail = (res.stdout or "")[-800:]
            logger.warning(f"figures: pdflatex no PDF\n{tail}")
            return False

        # 2) pdftoppm: конвертируем единственную страницу standalone в PNG.
        ppm_prefix = tmp / "fig_out"
        try:
            subprocess.run(
                [
                    "pdftoppm",
                    "-r", str(PREVIEW_DPI),
                    "-png",
                    "-singlefile",
                    str(pdf_path),
                    str(ppm_prefix),
                ],
                capture_output=True,
                check=True,
                timeout=20,
            )
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace") if e.stderr else ""
            logger.warning(f"figures: pdftoppm failed: {stderr}")
            return False
        except subprocess.TimeoutExpired:
            logger.error("figures: pdftoppm timeout")
            return False

        png_src = tmp / "fig_out.png"
        if not png_src.exists():
            logger.warning(f"figures: PNG не создан. Файлы: {list(tmp.iterdir())}")
            return False

        # 3) Обрезаем белые поля и записываем в персистентный кэш.
        out_png.write_bytes(_trim_white(png_src.read_bytes()))
        return True


# ── Асинхронный фасад для одного рисунка ────────────────────────────────────

async def compile_figure(tikz_body: str) -> Path | None:
    """Компилирует один TikZ-рисунок → PNG, с кэшированием по hash.

    Args:
        tikz_body: Сырой TikZ-код (без обёртки %%FIG/%%ENDFIG).

    Returns:
        Абсолютный Path к PNG-файлу или None при ошибке компиляции.
    """
    tikz_body = _sanitize_figure_body(tikz_body)  # снять float-обёртки (\caption и пр.)
    h = _figure_hash(tikz_body)
    out_png = FIGURE_CACHE_DIR / f"{h}.png"

    if out_png.exists():
        logger.info(f"figures: cache HIT {h}")
        return out_png

    logger.info(f"figures: compile START {h}, {len(tikz_body)} chars")
    async with _FIG_COMPILE_SEM:  # потолок параллельных pdflatex-процессов
        ok = await asyncio.to_thread(_compile_figure_sync, tikz_body, out_png)
    if not ok:
        logger.warning(f"figures: compile FAILED {h}")
        return None

    logger.info(f"figures: compile DONE {h}, {out_png.stat().st_size // 1024}KB")
    return out_png


# ── Основная функция замены блоков ──────────────────────────────────────────

async def render_figures_in_latex(latex: str) -> tuple[str, int, int]:
    """Находит все %%FIG...%%ENDFIG блоки, компилирует каждый в PNG и заменяет.

    При успехе блок заменяется на \\includegraphics с абсолютным путём к PNG.
    При неудаче блок молча удаляется — решение собирается без рисунка.

    Args:
        latex: LaTeX-текст решения с возможными %%FIG-блоками.

    Returns:
        Кортеж (новый_latex, n_ok, n_failed).
    """
    matches = list(FIG_RE.finditer(latex))
    if not matches:
        return latex, 0, 0

    # DoS-cap: компилируем максимум _MAX_FIGURES_PER_DOC блоков; лишние (i >= cap)
    # просто вырезаем (рисунок опционален) — иначе их сырой TikZ сломал бы рендер.
    capped = matches[:_MAX_FIGURES_PER_DOC]
    if len(matches) > _MAX_FIGURES_PER_DOC:
        logger.warning(f"figures: {len(matches)} блоков > cap {_MAX_FIGURES_PER_DOC} — лишние удалены")
    results: list[Path | None] = await asyncio.gather(
        *[compile_figure(m.group(1)) for m in capped]
    )

    n_ok = 0
    n_failed = 0
    # Заменяем справа налево, чтобы смещения не сбивались.
    for i in range(len(matches) - 1, -1, -1):
        match = matches[i]
        if i >= _MAX_FIGURES_PER_DOC:
            replacement = ""  # сверх лимита — убираем блок целиком
        elif (png_path := results[i]) is not None:
            replacement = (
                r"\begin{center}"
                r"\includegraphics"
                r"[width=0.8\linewidth,height=0.5\paperheight,keepaspectratio]"
                f"{{{str(png_path)}}}"
                r"\end{center}"
            )
            n_ok += 1
        else:
            # Рисунок не скомпилировался — убираем блок целиком.
            replacement = ""
            n_failed += 1
        latex = latex[: match.start()] + replacement + latex[match.end() :]

    logger.info(f"figures: {n_ok} ok, {n_failed} failed")
    return latex, n_ok, n_failed
