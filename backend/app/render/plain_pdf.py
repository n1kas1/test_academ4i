"""Plain-text → PDF через ReportLab (free-mode рендер).

Юзер видит чистое решение с Unicode-символами (∫ ∑ √ · и т.д.), без LaTeX и
без зависимости от pdflatex. Преимущества vs LaTeX-пути:

- Всегда компилируется (нет parser-ошибок типа \\cyrm in math mode).
- Не зовём Haiku/Sonnet-фиксы — нет денег на авто-починку.
- Быстрее (нет subprocess pdflatex + pdfcrop + pdftoppm цепочки).

Шрифт: DejaVu Sans (ставится через fonts-dejavu-core в Dockerfile). Покрывает
кириллицу + большинство Unicode math-символов. Fallback — Courier (built-in
у ReportLab, без кириллицы — нужен на dev-машине без DejaVu).
"""
import asyncio
import io
import os
import textwrap

from loguru import logger
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import portrait
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer


# Кастомная страница 16×22см (мобильно-удобный размер, как LaTeX-шаблон).
_PAGE_W = 16 * cm
_PAGE_H = 22 * cm
_PAGESIZE = portrait((_PAGE_W, _PAGE_H))


_BODY_FONT = "Body"
_BODY_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",         # Debian/Ubuntu
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",                  # Arch/Fedora
    "/Library/Fonts/Arial Unicode.ttf",                        # macOS (Cyrillic+math)
]
_FONTS_REGISTERED = False


def _register_fonts() -> str:
    """Регистрирует body-шрифт. Возвращает имя для использования в style."""
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return _BODY_FONT if _BODY_FONT in pdfmetrics.getRegisteredFontNames() else "Courier"
    for p in _BODY_PATHS:
        if os.path.exists(p):
            try:
                pdfmetrics.registerFont(TTFont(_BODY_FONT, p))
                logger.info(f"plain_pdf: registered body font {p}")
                _FONTS_REGISTERED = True
                return _BODY_FONT
            except Exception as e:
                logger.warning(f"plain_pdf: failed to register {p}: {e}")
    _FONTS_REGISTERED = True
    logger.warning("plain_pdf: no Unicode font found, falling back to Courier (no Cyrillic)")
    return "Courier"


# Safety-net hard-wrap: рубим супер-длинные "слова" без пробелов (формулы),
# чтобы они не уезжали за поле даже если ReportLab их не сможет перенести.
# Считаем по char count — приблизительно, основной перенос делает Paragraph
# по реальной пиксельной ширине.
MAX_LINE = 65


def _wrap_long_lines(text: str, width: int = MAX_LINE) -> str:
    out = []
    for raw in text.splitlines():
        if len(raw) <= width:
            out.append(raw)
            continue
        stripped = raw.lstrip(" ")
        indent = " " * (len(raw) - len(stripped))
        inner = max(20, width - len(indent))
        for w in textwrap.wrap(
            stripped, width=inner,
            break_long_words=True, break_on_hyphens=False,
            drop_whitespace=False, replace_whitespace=False,
        ):
            out.append(indent + w)
    return "\n".join(out)


def _to_paragraph_html(block: str) -> str:
    """Экранируем XML-спецсимволы и переводим \\n внутри блока в <br/>."""
    # Сначала экранируем, чтобы у нас не было реального XML в исходнике.
    esc = block.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Теперь \\n → <br/> (это уже наш разметочный тег, не пользовательский ввод).
    return esc.replace("\n", "<br/>")


def _append_figures(flow: list, figure_paths, content_w: float) -> None:
    """Встраивает скомпилированные PNG-рисунки в plain-PDF (чтобы рисунок не
    терялся, когда LaTeX-путь деградировал в plain-фолбэк). Масштаб по ширине."""
    for fp in figure_paths or ():
        try:
            iw, ih = ImageReader(str(fp)).getSize()
            scale = min(1.0, content_w / iw) if iw else 1.0
            flow.append(Spacer(1, 10))
            flow.append(RLImage(str(fp), width=iw * scale, height=ih * scale))
        except Exception as e:
            logger.warning(f"plain_pdf: не удалось вставить рисунок {fp}: {e}")


def _compile_sync(text: str, figure_paths=None) -> bytes:
    body_font = _register_fonts()
    text = _wrap_long_lines(text)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=_PAGESIZE,
        leftMargin=0.9 * cm, rightMargin=0.9 * cm,
        topMargin=0.9 * cm, bottomMargin=0.9 * cm,
        title="Academ4I solution",
    )
    body_style = ParagraphStyle(
        "PlainBody",
        fontName=body_font,
        fontSize=10.5,
        leading=14.5,
        textColor=HexColor("#111827"),
        wordWrap="CJK",  # CJK режим переносит ВЕЗДЕ при необходимости (не только по пробелам).
        allowOrphans=1,
        allowWidows=1,
    )
    # Делим текст на абзацы по пустой строке. Каждый абзац — отдельный Paragraph
    # (Paragraph переносит по реальной ширине шрифта, в отличие от XPreformatted).
    blocks = [b for b in text.split("\n\n") if b.strip()]
    flow = []
    for i, block in enumerate(blocks):
        flow.append(Paragraph(_to_paragraph_html(block), body_style))
        if i < len(blocks) - 1:
            flow.append(Spacer(1, 6))
    _append_figures(flow, figure_paths, _PAGE_W - 1.8 * cm)
    doc.build(flow)
    return buf.getvalue()


async def render_plain_pdf(text: str, figure_paths=None) -> dict:
    """Plain-text → PDF. Возвращает {pdf, preview_png=None, error}.

    figure_paths — пути к УЖЕ скомпилированным PNG-рисункам: встраиваются в конец,
    чтобы рисунок не пропадал, когда LaTeX-путь деградировал в plain-фолбэк.
    PNG-превью не делаем: Telegram сам показывает thumbnail PDF.
    """
    empty = {"pdf": None, "preview_png": None, "error": None}
    if not text or len(text.strip()) < 5:
        return {**empty, "error": "empty text"}
    try:
        pdf_bytes = await asyncio.to_thread(_compile_sync, text, figure_paths)
    except Exception as e:
        logger.exception(f"plain_pdf compile failed: {e}")
        return {**empty, "error": f"compile failed: {e}"}
    logger.info(f"plain_pdf DONE: pdf={len(pdf_bytes)/1024:.0f}KB")
    return {"pdf": pdf_bytes, "preview_png": None, "error": None}
