"""Таблица alert_log

Revision ID: aade0eb2c059
Revises: 6b0c386a248a
Create Date: 2026-08-21 17:48:32.563073
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'aade0eb2c059'
down_revision: Union[str, None] = '6b0c386a248a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('alert_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chat_id', sa.Integer(), nullable=False),
    sa.Column('opened_by_message_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('recipients', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['chat.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['opened_by_message_id'], ['message.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('chat_id', 'opened_by_message_id', 'kind', name='uq_alert_once')
    )


def downgrade() -> None:
    op.drop_table('alert_log')
