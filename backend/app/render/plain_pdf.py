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
import subprocess
import tempfile
from pathlib import Path

from loguru import logger
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, XPreformatted


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


def _escape_xml(s: str) -> str:
    """ReportLab XPreformatted понимает inline-XML — экранируем спецсимволы."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _compile_sync(text: str) -> bytes:
    body_font = _register_fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A5,
        leftMargin=1.0 * cm, rightMargin=1.0 * cm,
        topMargin=1.0 * cm, bottomMargin=1.0 * cm,
        title="Academ4I solution",
    )
    body_style = ParagraphStyle(
        "PlainBody",
        fontName=body_font,
        fontSize=10.5,
        leading=14.5,
        textColor=HexColor("#111827"),
        spaceAfter=0,
        wordWrap="LTR",
        allowOrphans=1,
        allowWidows=1,
    )
    # XPreformatted: сохраняет \n как переносы строк, при длинных строках сам
    # переносит по словам (в отличие от Preformatted — тот не переносит).
    flow = [XPreformatted(_escape_xml(text), body_style)]
    doc.build(flow)
    return buf.getvalue()


def _make_preview(pdf_bytes: bytes) -> bytes | None:
    """Первая страница PDF → PNG через pdftoppm (poppler уже в образе)."""
    try:
        with tempfile.TemporaryDirectory(prefix="plainpdf_") as td:
            t = Path(td)
            pdf_p = t / "in.pdf"
            pdf_p.write_bytes(pdf_bytes)
            subprocess.run(
                ["pdftoppm", "-r", "200", "-png", "-singlefile",
                 "-f", "1", "-l", "1", str(pdf_p), str(t / "out")],
                check=True, capture_output=True, timeout=10,
            )
            png_p = t / "out.png"
            return png_p.read_bytes() if png_p.exists() else None
    except Exception as e:
        logger.warning(f"plain_pdf preview failed (non-critical): {e}")
        return None


async def render_plain_pdf(text: str) -> dict:
    """Plain-text → PDF. Возвращает {pdf, preview_png, error}."""
    empty = {"pdf": None, "preview_png": None, "error": None}
    if not text or len(text.strip()) < 5:
        return {**empty, "error": "empty text"}
    try:
        pdf_bytes = await asyncio.to_thread(_compile_sync, text)
    except Exception as e:
        logger.exception(f"plain_pdf compile failed: {e}")
        return {**empty, "error": f"compile failed: {e}"}
    png_bytes = await asyncio.to_thread(_make_preview, pdf_bytes)
    logger.info(
        f"plain_pdf DONE: pdf={len(pdf_bytes)/1024:.0f}KB"
        + (f", png={len(png_bytes)/1024:.0f}KB" if png_bytes else " (no png)")
    )
    return {"pdf": pdf_bytes, "preview_png": png_bytes, "error": None}
