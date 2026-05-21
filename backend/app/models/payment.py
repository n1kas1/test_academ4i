"""Payment — лог платежей через TG Stars."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Integer, String, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Юзер
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # Stars-инфо
    telegram_payment_charge_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    amount_stars: Mapped[int] = mapped_column(Integer, nullable=False)

    # Что купил: "premium_30d"
    product: Mapped[str] = mapped_column(String(32), nullable=False)

    # Период подписки которая активировалась
    premium_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    premium_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Статус: "succeeded" / "refunded"
    status: Mapped[str] = mapped_column(String(16), default="succeeded", nullable=False)
    refunded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
