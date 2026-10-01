"""classification.attempts: счётчик попыток у строки отказа

Revision ID: b4e7c1a90d33
Revises: ab71c5d90e42
Create Date: 2026-09-21 15:30:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4e7c1a90d33"
down_revision: Union[str, None] = "ab71c5d90e42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "classification",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("classification", "attempts")
