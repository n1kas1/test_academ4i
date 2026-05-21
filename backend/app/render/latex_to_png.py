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
# Это инвалидирует все старые PNG-кэши автоматически.
TEMPLATE_VERSION = "v5"

# Узкая страница (14см) → крупный шрифт на iPhone. Запас по высоте, потом обрезаем через PIL.
LATEX_TEMPLATE = r"""\documentclass[12pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T2A]{fontenc}
\usepackage[russian]{babel}
\usepackage{amsmath,amssymb,amsthm,amsfonts,mathtools}
\usepackage{geometry}
\usepackage{xcolor}
\usepackage{enumitem}
\geometry{paperwidth=14cm,paperheight=80cm,margin=0.9cm,top=0.8cm,bottom=0.8cm}
\pagenumbering{gobble}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.65em}
\renewcommand{\baselinestretch}{1.2}

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

        # 2) PDF → PNG (DPI 200 — крупно и чётко). pdfcrop не используем,
        # обрежем белое через PIL независимо от tex-утилит.
        ppm_prefix = tmp / "out"
        try:
            subprocess.run(
                ["pdftoppm", "-r", "200", "-png", "-singlefile",
                 str(pdf_path), str(ppm_prefix)],
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

        # 3) Обрезаем белое поле через PIL (компенсация отсутствия pdfcrop).
        trimmed_bytes = _trim_white(png_src.read_bytes())

        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(trimmed_bytes)
        return True


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
