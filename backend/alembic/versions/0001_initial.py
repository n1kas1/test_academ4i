"""Initial migration: pgvector + users + solutions + payments

Revision ID: 0001
Revises:
Create Date: 2026-05-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Расширение pgvector. У Supabase оно ставится в schema "extensions"
    #    через UI (Database → Extensions). Тут IF NOT EXISTS — на случай локального запуска.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA extensions")
    # search_path для миграции — чтобы тип vector нашёлся без префикса
    op.execute("SET search_path TO public, extensions")

    # 2) users
    op.create_table(
        "users",
        sa.Column("telegram_id", sa.BigInteger, primary_key=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("first_name", sa.String(128), nullable=True),
        sa.Column("last_name", sa.String(128), nullable=True),
        sa.Column("language_code", sa.String(8), nullable=True),
        sa.Column("total_solved", sa.Integer, nullable=False, server_default="0"),
        sa.Column("free_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("premium_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_users_premium_until", "users", ["premium_until"])

    # 3) solutions (кэш + чанки учебников)
    op.create_table(
        "solutions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_text", sa.Text, nullable=False),
        sa.Column("task_latex", sa.Text, nullable=True),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("topic", sa.String(32), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("solution_markdown", sa.Text, nullable=False),
        sa.Column("usage_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("generated_for_user", sa.BigInteger, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_solutions_topic", "solutions", ["topic"])
    op.create_index("ix_solutions_source", "solutions", ["source"])

    # HNSW индекс для быстрого cosine search в pgvector
    op.execute(
        "CREATE INDEX ix_solutions_embedding_hnsw ON solutions "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # 4) payments (лог Stars-платежей)
    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("telegram_id", sa.BigInteger, nullable=False),
        sa.Column("telegram_payment_charge_id", sa.String(128), nullable=False, unique=True),
        sa.Column("amount_stars", sa.Integer, nullable=False),
        sa.Column("product", sa.String(32), nullable=False),
        sa.Column("premium_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("premium_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="succeeded"),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_payments_telegram_id", "payments", ["telegram_id"])


def downgrade() -> None:
    op.drop_table("payments")
    op.execute("DROP INDEX IF EXISTS ix_solutions_embedding_hnsw")
    op.drop_table("solutions")
    op.drop_table("users")
    op.execute("DROP EXTENSION IF EXISTS vector")
