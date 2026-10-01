"""bot_user.staff_id: частичный уникальный индекс

Revision ID: 7a1c4e90b52d
Revises: 5f2a71c0d38e
Create Date: 2026-08-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "7a1c4e90b52d"
down_revision: Union[str, None] = "5f2a71c0d38e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_bot_user_staff",
        "bot_user",
        ["staff_id"],
        unique=True,
        postgresql_where=sa.text("staff_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_bot_user_staff", table_name="bot_user")
