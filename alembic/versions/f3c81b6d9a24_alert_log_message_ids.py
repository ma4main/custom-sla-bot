"""alert_log: id отправленных сообщений, их текст и отметка о зачёркивании

Revision ID: f3c81b6d9a24
Revises: e5a91c47b3d0
Create Date: 2026-09-07 11:10:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'f3c81b6d9a24'
down_revision: Union[str, None] = 'e5a91c47b3d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'alert_log',
        sa.Column(
            'message_ids',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column('alert_log', sa.Column('sent_text', sa.Text(), nullable=True))
    op.add_column(
        'alert_log',
        sa.Column('struck_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('alert_log', 'struck_at')
    op.drop_column('alert_log', 'sent_text')
    op.drop_column('alert_log', 'message_ids')
