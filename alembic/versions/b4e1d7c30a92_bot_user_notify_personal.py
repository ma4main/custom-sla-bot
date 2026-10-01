"""bot_user.notify_personal: уведомления в личку

Revision ID: b4e1d7c30a92
Revises: d2bd5cac5070
Create Date: 2026-09-01 20:40:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4e1d7c30a92'
down_revision: Union[str, None] = 'd2bd5cac5070'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'bot_user',
        sa.Column('notify_personal', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('bot_user', 'notify_personal')
