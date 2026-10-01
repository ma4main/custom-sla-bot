"""classification.source и состояние доставки алертов в alert_log

Revision ID: 3d5e8b21c4a7
Revises: 2c4f7a9e10b3
Create Date: 2026-08-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "3d5e8b21c4a7"
down_revision: Union[str, None] = "2c4f7a9e10b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "classification",
        sa.Column(
            "source", sa.String(length=16), server_default="model", nullable=False
        ),
    )
    op.add_column(
        "alert_log",
        sa.Column("delivered", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column(
        "alert_log",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("alert_log", sa.Column("last_error", sa.Text(), nullable=True))

    op.alter_column("alert_log", "delivered", server_default="false")


def downgrade() -> None:
    op.drop_column("alert_log", "last_error")
    op.drop_column("alert_log", "attempts")
    op.drop_column("alert_log", "delivered")
    op.drop_column("classification", "source")
