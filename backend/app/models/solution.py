"""Solution — кэш решённых задач с эмбеддингами для RAG."""
import uuid
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Solution(Base, TimestampMixin):
    """Универсальное хранилище:
    - чанки учебников (source = "Демидович" / "Кострикин" / "Антидемидович-Т1" / ...)
    - решения сгенерированные через Claude (source = "generated")
    """
    __tablename__ = "solutions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Условие задачи (текст после OCR)
    task_text: Mapped[str] = mapped_column(Text, nullable=False)
    task_latex: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Эмбеддинг условия (1536 — text-embedding-3-small)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)

    # Тема: matan / lin_alg / groups / rings_fields / polynomials
    topic: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # Откуда: "Демидович" / "Кострикин" / "generated"
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Само решение (MarkdownV2 для Telegram)
    solution_markdown: Mapped[str] = mapped_column(Text, nullable=False)

    # Статистика — сколько раз эта задача была отдана юзерам
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Кто сгенерировал (если source=generated) — для аналитики
    generated_for_user: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
