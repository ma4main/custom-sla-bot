"""Таблица breach_dismissal

Revision ID: d2bd5cac5070
Revises: 2842e2f27f45
Create Date: 2026-08-28 20:05:42.171050
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2bd5cac5070'
down_revision: Union[str, None] = '2842e2f27f45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('breach_dismissal',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chat_id', sa.Integer(), nullable=False),
    sa.Column('opened_by_message_id', sa.Integer(), nullable=False),
    sa.Column('dismissed_by', sa.Integer(), nullable=True),
    sa.Column('dismissed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['chat.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['dismissed_by'], ['bot_user.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['opened_by_message_id'], ['message.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('chat_id', 'opened_by_message_id', name='uq_breach_dismissal')
    )


def downgrade() -> None:
    op.drop_table('breach_dismissal')
