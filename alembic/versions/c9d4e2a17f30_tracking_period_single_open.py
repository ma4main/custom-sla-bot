"""Один открытый интервал наблюдения на чат

Revision ID: c9d4e2a17f30
Revises: b4e1d7c30a92
Create Date: 2026-09-02 20:30:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9d4e2a17f30'
down_revision: Union[str, None] = 'b4e1d7c30a92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE chat_tracking_period AS older
            SET ended_at = newer.started_at
            FROM chat_tracking_period AS newer
            WHERE older.chat_id = newer.chat_id
              AND older.ended_at IS NULL
              AND newer.ended_at IS NULL
              AND newer.started_at > older.started_at
            """
        )
    )
    op.create_index(
        'uq_tracking_period_open',
        'chat_tracking_period',
        ['chat_id'],
        unique=True,
        postgresql_where=sa.text('ended_at IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_tracking_period_open', table_name='chat_tracking_period')
