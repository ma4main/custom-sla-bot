"""alert_log: колонка shadow

Revision ID: 1b9d0812b62a
Revises: 0a912e783710
Create Date: 2026-08-24 16:01:24.430016
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '1b9d0812b62a'
down_revision: Union[str, None] = '0a912e783710'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('alert_log', sa.Column('shadow', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('alert_log', 'shadow')
