"""interaction: момент передачи вопроса специалисту

Revision ID: 8b3d2f61ca04
Revises: 7a1c4e90b52d
Create Date: 2026-08-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8b3d2f61ca04"
down_revision: Union[str, None] = "7a1c4e90b52d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "interaction",
        sa.Column("handoff_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "interaction",
        sa.Column("handoff_staff_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "interaction_handoff_staff_id_fkey",
        "interaction",
        "staff",
        ["handoff_staff_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "interaction_handoff_staff_id_fkey", "interaction", type_="foreignkey"
    )
    op.drop_column("interaction", "handoff_staff_id")
    op.drop_column("interaction", "handoff_at")
