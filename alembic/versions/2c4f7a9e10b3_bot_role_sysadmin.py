"""bot_role: значение SYSADMIN (вручную: autogenerate не видит новых значений enum)

Revision ID: 2c4f7a9e10b3
Revises: 1b9d0812b62a
Create Date: 2026-08-24
"""
from typing import Sequence, Union

from alembic import op

revision: str = "2c4f7a9e10b3"
down_revision: Union[str, None] = "1b9d0812b62a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE bot_role ADD VALUE IF NOT EXISTS 'SYSADMIN'")


def downgrade() -> None:
    # PostgreSQL не удаляет значения из enum; значение безвредно — остаётся.
    pass
