"""staff: наблюдаемая роль сотрудника и её источник

Revision ID: 2842e2f27f45
Revises: 8b3d2f61ca04
Create Date: 2026-08-27 12:27:45.575083
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2842e2f27f45'
down_revision: Union[str, None] = '8b3d2f61ca04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('staff', sa.Column('role', sa.String(length=16), nullable=True))
    op.add_column('staff', sa.Column('role_source', sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column('staff', 'role_source')
    op.drop_column('staff', 'role')
