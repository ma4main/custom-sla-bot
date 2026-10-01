"""chat_tracking_period: история интервалов наблюдения за чатом

Revision ID: 4e7c1a93f2b8
Revises: 3d5e8b21c4a7
Create Date: 2026-08-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4e7c1a93f2b8"
down_revision: Union[str, None] = "3d5e8b21c4a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_tracking_period",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["chat_id"], ["chat.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tracking_period_chat", "chat_tracking_period", ["chat_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_tracking_period_chat", table_name="chat_tracking_period")
    op.drop_table("chat_tracking_period")
