"""LaTeX → PNG рендер.

Принимает LaTeX-фрагмент (решение задачи от Claude), компилирует через pdflatex,
конвертирует в PNG. Кэш по хэшу содержимого — повторный рендер мгновенный.

Зависимости (Dockerfile): texlive-latex-* + poppler-utils (pdftoppm).
"""
import asyncio
import hashlib
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

CACHE_DIR = Path("/app/render_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Базовый шаблон. Геометрия A5-подобная для одностраничного решения.
# Если решение длинное — PDF автоматически переносит, мы потом склеиваем PNG.
LATEX_TEMPLATE = r"""\documentclass[12pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T2A]{fontenc}
\usepackage[russian]{babel}
\usepackage{amsmath,amssymb,amsthm,amsfonts,mathtools}
\usepackage{geometry}
\usepackage{xcolor}
\usepackage{enumitem}
\geometry{paperwidth=16cm,paperheight=50cm,margin=1cm,top=1cm,bottom=1cm}
\pagenumbering{gobble}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.4em}
\renewcommand{\baselinestretch}{1.15}
\definecolor{accent}{HTML}{2563eb}
\definecolor{ok}{HTML}{059669}
\newcommand{\hd}[1]{\textcolor{accent}{\textbf{\large #1}}\\[-0.2em]\rule{\linewidth}{0.4pt}\\}
\newcommand{\ans}[1]{\textcolor{ok}{\textbf{Ответ:}} \boxed{#1}}
\begin{document}
%CONTENT%
\end{document}
"""


def _content_hash(latex: str) -> str:
    return hashlib.sha256(latex.encode("utf-8")).hexdigest()[:16]


def _compile_sync(latex_content: str, out_png: Path) -> bool:
    """Синхронная компиляция: latex → pdflatex → pdftoppm → PNG.
    Возвращает True если успешно.
    """
    with tempfile.TemporaryDirectory(prefix="latex_") as tmpdir:
        tmp = Path(tmpdir)
        tex_path = tmp / "doc.tex"
        full_doc = LATEX_TEMPLATE.replace("%CONTENT%", latex_content)
        tex_path.write_text(full_doc, encoding="utf-8")

        # pdflatex — две прохода для крестных ссылок (нам не критично, одного хватит)
        try:
            res = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                 "-output-directory", str(tmp), str(tex_path)],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            logger.error("pdflatex timeout")
            return False

        pdf_path = tmp / "doc.pdf"
        if not pdf_path.exists():
            # Лог последних строк stderr/stdout pdflatex
            tail = (res.stdout or "")[-800:]
            logger.error(f"pdflatex failed:\n{tail}")
            return False

        # PDF → PNG (одна "страница" т.к. paperheight большой)
        # pdftoppm -r 150 даёт DPI 150 — норм для Telegram
        ppm_prefix = tmp / "out"
        try:
            subprocess.run(
                ["pdftoppm", "-r", "150", "-png", "-singlefile", str(pdf_path), str(ppm_prefix)],
                capture_output=True, check=True, timeout=20,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"pdftoppm failed: {e.stderr.decode() if e.stderr else ''}")
            return False
        except subprocess.TimeoutExpired:
            logger.error("pdftoppm timeout")
            return False

        png_src = tmp / "out.png"
        if not png_src.exists():
            logger.error(f"PNG not produced. Files in tmp: {list(tmp.iterdir())}")
            return False

        # Перенос в кэш
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(png_src.read_bytes())
        return True


async def render_latex_to_png(latex_content: str) -> bytes | None:
    """Асинхронный фасад: рендерит LaTeX в PNG, использует кэш по хэшу контента.

    Возвращает байты PNG или None при ошибке.
    """
    if not latex_content or len(latex_content.strip()) < 10:
        logger.warning("LaTeX content empty")
        return None

    h = _content_hash(latex_content)
    cached = CACHE_DIR / f"{h}.png"
    if cached.exists():
        logger.info(f"PNG cache HIT: {h}")
        return cached.read_bytes()

    logger.info(f"PNG render START: {h}, {len(latex_content)} chars")
    ok = await asyncio.to_thread(_compile_sync, latex_content, cached)
    if not ok:
        return None

    data = cached.read_bytes()
    logger.info(f"PNG render DONE: {h}, {len(data)/1024:.0f}KB")
    return data
