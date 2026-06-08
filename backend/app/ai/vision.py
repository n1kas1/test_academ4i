"""Утилиты для работы с фото.

Claude Vision принимает изображение в base64 как media-block в content.
Никакого отдельного OCR — Claude видит фото и сразу решает задачу.

Тут — только подготовка фото: ресайз, нормализация формата, кодирование в base64.
"""
import base64
import io

from PIL import Image
from loguru import logger

# Защита от decompression-bomb: PIL по достижении лимита бросает
# DecompressionBombError ДО разворачивания в память. 25 Мпикс с запасом
# перекрывают реальные фото задач (~12 Мпикс), но не дают «бомбе» 50000×50000
# съесть RAM (на VPS ~1GB это мгновенный OOM). ДОЛЖНО стоять до Image.open.
Image.MAX_IMAGE_PIXELS = 25_000_000

# Claude поддерживает: JPEG, PNG, GIF, WEBP. Макс 5MB на изображение.
MAX_SIDE = 1568              # рекомендация Anthropic: длинная сторона <= 1568px
MAX_BYTES = 4 * 1024 * 1024  # запас от лимита 5MB
JPEG_QUALITY = 85


def prepare_image(image_bytes: bytes) -> tuple[str, str]:
    """Подготовить фото от юзера для Claude Vision.

    Возвращает (base64_data, media_type) — пригодные для передачи в Anthropic SDK
    как {"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}.
    """
    img = Image.open(io.BytesIO(image_bytes))

    # 1) Конвертим в RGB (PNG с альфой → JPEG-совместимо)
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # 2) Уменьшаем длинную сторону до MAX_SIDE
    w, h = img.size
    if max(w, h) > MAX_SIDE:
        scale = MAX_SIDE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # 3) Сжимаем JPEG — итерируем quality если превышает лимит
    quality = JPEG_QUALITY
    while True:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= MAX_BYTES or quality <= 50:
            break
        quality -= 10

    b64 = base64.b64encode(data).decode()
    logger.info(
        f"Image prepared: {img.size}, {len(data)/1024:.0f}KB, quality={quality}"
    )
    return b64, "image/jpeg"
