"""User — пользователь бота."""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Integer, String, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    # Telegram user_id — основной ключ (BIGINT, у TG большие числа)
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Профиль из Telegram
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    language_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    # Счётчик решений
    total_solved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Free tier: N задач lifetime (FREE_LIFETIME_TASKS)
    free_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Credits — задачи, купленные пакетом (поверх free, расходуются ПЕРЕД free)
    credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Premium подписка
    premium_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def has_premium(self, now: datetime) -> bool:
        return self.premium_until is not None and self.premium_until > now

    def free_remaining(self, free_limit: int) -> int:
        return max(0, free_limit - self.free_used)

    def __repr__(self) -> str:
        return f"<User tg={self.telegram_id} solved={self.total_solved} premium={self.premium_until}>"
