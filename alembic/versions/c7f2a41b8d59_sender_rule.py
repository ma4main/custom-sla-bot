"""Таблица sender_rule: решения человека о стороне отправителя

Revision ID: c7f2a41b8d59
Revises: b4e7c1a90d33
Create Date: 2026-09-22 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c7f2a41b8d59"
down_revision: Union[str, None] = "b4e7c1a90d33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Типы создаются и удаляются отдельно от таблицы, поэтому create_type=False.
_KIND = postgresql.ENUM(
    "TG_USER", "BOT", "ANONYMOUS_ADMIN", name="sender_rule_kind", create_type=False
)
_SIDE = postgresql.ENUM(
    "COMPANY", "CLIENT", "SYSTEM", name="sender_rule_side", create_type=False
)


def upgrade() -> None:
    op.execute("CREATE TYPE sender_rule_kind AS ENUM ('TG_USER', 'BOT', 'ANONYMOUS_ADMIN')")
    op.execute("CREATE TYPE sender_rule_side AS ENUM ('COMPANY', 'CLIENT', 'SYSTEM')")
    op.create_table(
        "sender_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", _KIND, nullable=False),
        sa.Column("key", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=True),
        sa.Column("side", _SIDE, nullable=False),
        sa.Column("staff_id", sa.Integer(), nullable=True),
        sa.Column("decided_by", sa.Integer(), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("display", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["chat_id"], ["chat.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["staff_id"], ["staff.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by"], ["bot_user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "ALTER TABLE sender_rule ADD CONSTRAINT uq_sender_rule "
        "UNIQUE NULLS NOT DISTINCT (kind, key, chat_id)"
    )


def downgrade() -> None:
    op.drop_table("sender_rule")
    op.execute("DROP TYPE sender_rule_side")
    op.execute("DROP TYPE sender_rule_kind")
