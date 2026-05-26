"""Credit-model migration: bonus + subscription conversion (data-only)

Однократное начисление при переходе на credit-модель (вариант A):
  • +10 кредитов всем существующим юзерам (приветственный бонус);
  • активным подписчикам (premium_until > NOW()) — конвертация остатка срока
    в кредиты по ставке 3 кредита за полный оставшийся день.

Схема НЕ меняется: используется существующая колонка users.credits.
Колонки premium_until/free_* остаются (не используются логикой, не дропаем).

Числа согласованы с владельцем (6 юзеров < 500 → бонус +10; конвертация ×3).

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-26
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # +10 всем; активным подпискам ещё +3 за каждый полный оставшийся день.
    op.execute("""
        UPDATE users
        SET credits = credits
            + 10
            + CASE
                WHEN premium_until > NOW() THEN
                    GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (premium_until - NOW())) / 86400))::int * 3
                ELSE 0
              END
    """)


def downgrade() -> None:
    # Data-миграция начисления — надёжно не реверсится (нельзя отличить
    # бонусные кредиты от потраченных/докупленных). Откат не выполняем.
    pass
