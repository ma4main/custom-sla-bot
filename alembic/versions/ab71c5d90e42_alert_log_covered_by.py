"""alert_log.covered_by_id: не больше одного открытого алерта слоя на чат

Revision ID: ab71c5d90e42
Revises: 9c2f4e81ab07
Create Date: 2026-09-16 09:30:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ab71c5d90e42"
down_revision: Union[str, None] = "9c2f4e81ab07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("alert_log", sa.Column("covered_by_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_alert_log_covered_by_id"), "alert_log", ["covered_by_id"], unique=False
    )
    op.create_foreign_key(
        "fk_alert_log_covered_by_id_alert_log",
        "alert_log",
        "alert_log",
        ["covered_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_alert_log_covered_by_id_alert_log", "alert_log", type_="foreignkey"
    )
    op.drop_index(op.f("ix_alert_log_covered_by_id"), table_name="alert_log")
    op.drop_column("alert_log", "covered_by_id")
