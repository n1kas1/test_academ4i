"""LaTeX → PNG рендер.

Pipeline:
  LaTeX-фрагмент → шаблон → pdflatex → pdfcrop (обрезка пустоты) → pdftoppm → PNG.
Кэш по hash контента — повтор мгновенный.

Dockerfile-зависимости: texlive-latex-* + texlive-extra-utils (pdfcrop) + poppler-utils (pdftoppm).
"""
import asyncio
import hashlib
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

CACHE_DIR = Path("/app/render_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Шаблон: широкая страница 18см, большой запас по высоте, потом обрезаем pdfcrop.
LATEX_TEMPLATE = r"""\documentclass[12pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T2A]{fontenc}
\usepackage[russian]{babel}
\usepackage{amsmath,amssymb,amsthm,amsfonts,mathtools}
\usepackage{geometry}
\usepackage{xcolor}
\usepackage{enumitem}
\geometry{paperwidth=18cm,paperheight=80cm,margin=1.2cm,top=1cm,bottom=1cm}
\pagenumbering{gobble}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.6em}
\renewcommand{\baselinestretch}{1.2}

\definecolor{accent}{HTML}{1d4ed8}
\definecolor{accentline}{HTML}{c7d2fe}
\definecolor{ok}{HTML}{047857}
\definecolor{okbg}{HTML}{d1fae5}

% Заголовок секции — крупный синий + тонкая полоса под ним
\newcommand{\hd}[1]{%
  \vspace{0.8em}\noindent%
  {\color{accent}\textbf{\Large #1}}%
  \par\vspace{-0.15em}%
  \noindent\textcolor{accentline}{\rule{\linewidth}{1.2pt}}%
  \par\vspace{0.4em}%
}

% Ответ — центрированный зелёный блок
\newcommand{\ans}[1]{%
  \par\vspace{0.7em}%
  \begin{center}
  \fcolorbox{ok}{okbg}{%
    \begin{minipage}{0.92\linewidth}\centering%
      {\color{ok}\textbf{\large Ответ:}}\quad\boxed{#1}%
    \end{minipage}%
  }%
  \end{center}%
  \vspace{0.5em}%
}

% Альтернативная команда для шага решения — единообразное оформление
\newcommand{\stp}[2]{\textbf{Шаг #1.}\quad #2}

\begin{document}
%CONTENT%
\end{document}
"""


def _content_hash(latex: str) -> str:
    return hashlib.sha256(latex.encode("utf-8")).hexdigest()[:16]


def _compile_sync(latex_content: str, out_png: Path) -> bool:
    """Синхронно: latex → pdflatex → pdfcrop → pdftoppm → PNG.
    Возвращает True если успешно.
    """
    with tempfile.TemporaryDirectory(prefix="latex_") as tmpdir:
        tmp = Path(tmpdir)
        tex_path = tmp / "doc.tex"
        full_doc = LATEX_TEMPLATE.replace("%CONTENT%", latex_content)
        tex_path.write_text(full_doc, encoding="utf-8")

        # 1) pdflatex
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
            tail = (res.stdout or "")[-800:]
            logger.error(f"pdflatex failed:\n{tail}")
            return False

        # 2) pdfcrop — обрезаем пустоту снизу. Если не получится, продолжим с обычным PDF.
        cropped_pdf = tmp / "doc-crop.pdf"
        try:
            subprocess.run(
                ["pdfcrop", "--margins", "8 8 8 8", str(pdf_path), str(cropped_pdf)],
                capture_output=True, check=True, timeout=15,
            )
            source_pdf = cropped_pdf if cropped_pdf.exists() else pdf_path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning(f"pdfcrop unavailable, using uncropped: {e}")
            source_pdf = pdf_path

        # 3) PDF → PNG, DPI 170 для хорошей чёткости
        ppm_prefix = tmp / "out"
        try:
            subprocess.run(
                ["pdftoppm", "-r", "170", "-png", "-singlefile",
                 str(source_pdf), str(ppm_prefix)],
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
            logger.error(f"PNG not produced. Files: {list(tmp.iterdir())}")
            return False

        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(png_src.read_bytes())
        return True


async def render_latex_to_png(latex_content: str) -> bytes | None:
    """Асинхронный фасад. Кэш по hash контента. Возвращает байты PNG или None."""
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
