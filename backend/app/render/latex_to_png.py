"""LaTeX → PNG рендер.

Pipeline:
  LaTeX-фрагмент → шаблон → pdflatex → pdfcrop (обрезка пустоты) → pdftoppm → PNG.
Кэш по hash контента — повтор мгновенный.

Dockerfile-зависимости: texlive-latex-* + texlive-extra-utils (pdfcrop) + poppler-utils (pdftoppm).
"""
import asyncio
import hashlib
import io
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops
from loguru import logger

CACHE_DIR = Path("/app/render_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Версия шаблона — инкрементим при любом изменении LATEX_TEMPLATE.
# Это инвалидирует все старые кэши автоматически.
TEMPLATE_VERSION = "v8"

# Страница A5-формата (14×22см): узкая → крупный шрифт на телефоне, а нормальная
# высота → LaTeX сам разбивает длинное решение на несколько страниц (раньше была
# одна 80-см страница, и хвост с ответом терялся при конвертации).
LATEX_TEMPLATE = r"""\documentclass[12pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T2A]{fontenc}
\usepackage[russian]{babel}
\usepackage{amsmath,amssymb,amsthm,amsfonts,mathtools}
\usepackage{mathrsfs}  % \mathscr — сигма-алгебры/нотация тервера (часто у Claude)
\usepackage{geometry}
\usepackage{xcolor}
\usepackage{enumitem}
\geometry{paperwidth=16cm,paperheight=22cm,margin=0.6cm,top=0.8cm,bottom=0.8cm}
\pagenumbering{gobble}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.65em}
\renewcommand{\baselinestretch}{1.2}
% Меньше overfull на длинных строках/формулах: тянем пробелы и допускаем
% более свободные переносы (\sloppy), чтобы текст не вылезал за край.
\setlength{\emergencystretch}{3em}
\sloppy

\definecolor{accent}{HTML}{1d4ed8}
\definecolor{accentline}{HTML}{a5b4fc}
\definecolor{ok}{HTML}{047857}
\definecolor{okbg}{HTML}{d1fae5}

% Заголовок секции — ЛИНИЯ СВЕРХУ + крупный синий текст.
% Линия играет роль разделителя между секциями.
\newcommand{\hd}[1]{%
  \vspace{1.1em}%
  \noindent\textcolor{accentline}{\rule{\linewidth}{1.3pt}}%
  \par\vspace{0.25em}%
  {\color{accent}\textbf{\Large #1}}%
  \par\vspace{0.5em}%
}

% Ответ — просто крупный boxed по центру, без фона.
% Слово "Ответ" уже в \hd{Ответ} выше — дублировать не надо.
\newcommand{\ans}[1]{%
  \par\vspace{0.6em}%
  \begin{center}%
    \large\boxed{#1}%
  \end{center}%
  \vspace{0.5em}%
}

\newcommand{\stp}[2]{\textbf{Шаг #1.}\quad #2}

\begin{document}
%CONTENT%
\end{document}
"""


def _content_hash(latex: str) -> str:
    """Хэш зависит от версии шаблона — изменение шаблона инвалидирует все кэши."""
    key = f"{TEMPLATE_VERSION}:{latex}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# DPI превью первой страницы (выше = чётче формулы; 300 ≈ 1654px по ширине 14см).
PREVIEW_DPI = 300


def _compile_sync(latex_content: str, out_pdf: Path, out_png: Path) -> tuple[bool, str]:
    """Синхронно: latex → pdflatex → PDF + PNG-превью первой страницы.

    Пишет полный PDF в out_pdf и превью 1-й страницы в out_png.
    Возвращает (ok, error): error — хвост лога pdflatex при провале (для авто-фикса).
    """
    with tempfile.TemporaryDirectory(prefix="latex_") as tmpdir:
        tmp = Path(tmpdir)
        tex_path = tmp / "doc.tex"
        full_doc = LATEX_TEMPLATE.replace("%CONTENT%", latex_content)
        tex_path.write_text(full_doc, encoding="utf-8")

        # 1) pdflatex. -no-shell-escape — контент идёт от LLM, отключаем \write18.
        try:
            res = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                 "-no-shell-escape", "-output-directory", str(tmp), str(tex_path)],
                # errors="replace": лог pdflatex может содержать не-UTF-8 байты
                # (cp1251/T2A в предупреждениях) — строгий декод иначе роняет рендер.
                capture_output=True, encoding="utf-8", errors="replace", timeout=30,
            )
        except subprocess.TimeoutExpired:
            logger.error("pdflatex timeout")
            return False, "pdflatex timeout"

        pdf_path = tmp / "doc.pdf"
        if not pdf_path.exists():
            tail = (res.stdout or "")[-1200:]
            logger.error(f"pdflatex failed:\n{tail}")
            return False, tail

        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.write_bytes(pdf_path.read_bytes())

        # 2) Превью ПЕРВОЙ страницы → PNG. Полное решение остаётся в PDF.
        ppm_prefix = tmp / "out"
        try:
            subprocess.run(
                ["pdftoppm", "-r", str(PREVIEW_DPI), "-png", "-singlefile",
                 str(pdf_path), str(ppm_prefix)],
                capture_output=True, check=True, timeout=20,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"pdftoppm failed: {e.stderr.decode() if e.stderr else ''}")
            return False, "pdftoppm failed"
        except subprocess.TimeoutExpired:
            logger.error("pdftoppm timeout")
            return False, "pdftoppm timeout"

        png_src = tmp / "out.png"
        if not png_src.exists():
            logger.error(f"PNG not produced. Files: {list(tmp.iterdir())}")
            return False, "PNG not produced"

        # 3) Обрезаем белое поле превью через PIL.
        out_png.write_bytes(_trim_white(png_src.read_bytes()))
        return True, ""


def _trim_white(png_bytes: bytes, padding: int = 20) -> bytes:
    """Обрезает белые поля вокруг изображения. Оставляет padding пикселей с каждой стороны."""
    img = Image.open(io.BytesIO(png_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    bg = Image.new("RGB", img.size, (255, 255, 255))
    diff = ImageChops.difference(img, bg)
    bbox = diff.getbbox()
    if bbox is None:
        # Полностью белая картинка — возвращаем как есть
        return png_bytes
    # Добавим padding но не выйдем за границы оригинала
    left, top, right, bottom = bbox
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(img.width, right + padding)
    bottom = min(img.height, bottom + padding)
    trimmed = img.crop((left, top, right, bottom))
    buf = io.BytesIO()
    trimmed.save(buf, format="PNG", optimize=True)
    logger.info(f"PNG trimmed: {img.size} → {trimmed.size}")
    return buf.getvalue()


async def render_solution(latex_content: str) -> dict:
    """Асинхронный фасад. Кэш по hash контента.

    Возвращает {"pdf": bytes|None, "preview_png": bytes|None, "error": str|None}:
      • pdf         — полное решение (векторное, многостраничное);
      • preview_png — PNG-превью первой страницы (для инлайн-показа в чате);
      • error       — хвост лога pdflatex при провале (для авто-фикса LaTeX), иначе None.
    """
    empty = {"pdf": None, "preview_png": None, "error": None}
    if not latex_content or len(latex_content.strip()) < 10:
        logger.warning("LaTeX content empty")
        return empty

    h = _content_hash(latex_content)
    pdf_path = CACHE_DIR / f"{h}.pdf"
    png_path = CACHE_DIR / f"{h}.png"
    if pdf_path.exists() and png_path.exists():
        logger.info(f"render cache HIT: {h}")
        return {"pdf": pdf_path.read_bytes(), "preview_png": png_path.read_bytes(), "error": None}

    logger.info(f"render START: {h}, {len(latex_content)} chars")
    ok, err = await asyncio.to_thread(_compile_sync, latex_content, pdf_path, png_path)
    if not ok:
        return {"pdf": None, "preview_png": None, "error": err}

    pdf_bytes = pdf_path.read_bytes()
    png_bytes = png_path.read_bytes()
    logger.info(
        f"render DONE: {h}, pdf={len(pdf_bytes)/1024:.0f}KB, png={len(png_bytes)/1024:.0f}KB"
    )
    return {"pdf": pdf_bytes, "preview_png": png_bytes, "error": None}
