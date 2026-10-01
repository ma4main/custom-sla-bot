"""interaction: якоря сообщений реакции и ответа

Revision ID: d1f7a3b5c802
Revises: c9d4e2a17f30
Create Date: 2026-09-02 21:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1f7a3b5c802'
down_revision: Union[str, None] = 'c9d4e2a17f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'interaction',
        sa.Column(
            'first_reaction_message_id',
            sa.Integer(),
            sa.ForeignKey('message.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )
    op.add_column(
        'interaction',
        sa.Column(
            'substantive_message_id',
            sa.Integer(),
            sa.ForeignKey('message.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('interaction', 'substantive_message_id')
    op.drop_column('interaction', 'first_reaction_message_id')
