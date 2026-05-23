"""User — пользователь бота."""
from datetime import datetime, timedelta
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

    # Free tier: задачи, использованные В ТЕКУЩЕМ окне (free_window_start).
    free_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Начало текущего бесплатного окна. NULL → окно ещё не открыто (доступен
    # полный лимит). По истечении free_window_days окно считается сброшенным.
    free_window_start: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Credits — задачи, купленные пакетом (поверх free, расходуются ПЕРЕД free)
    credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Premium подписка
    premium_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def has_premium(self, now: datetime) -> bool:
        return self.premium_until is not None and self.premium_until > now

    def _window_expired(self, now: datetime, window_days: int) -> bool:
        return (
            self.free_window_start is None
            or (now - self.free_window_start) >= timedelta(days=window_days)
        )

    def free_remaining(self, now: datetime, limit: int, window_days: int) -> int:
        """Сколько бесплатных задач доступно сейчас. Окно истекло → полный лимит."""
        if self._window_expired(now, window_days):
            return limit
        return max(0, limit - self.free_used)

    def free_resets_at(self, window_days: int) -> Optional[datetime]:
        """Когда обновится бесплатный лимит (None, если окно ещё не открыто)."""
        if self.free_window_start is None:
            return None
        return self.free_window_start + timedelta(days=window_days)

    def __repr__(self) -> str:
        return f"<User tg={self.telegram_id} solved={self.total_solved} premium={self.premium_until}>"
