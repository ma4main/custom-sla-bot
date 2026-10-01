"""interaction: колонка substantive_breached

Revision ID: 0a912e783710
Revises: aade0eb2c059
Create Date: 2026-08-24 15:08:33.642312
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0a912e783710'
down_revision: Union[str, None] = 'aade0eb2c059'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('interaction', sa.Column('substantive_breached', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('interaction', 'substantive_breached')
