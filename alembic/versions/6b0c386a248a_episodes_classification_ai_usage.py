"""Таблицы interaction, classification и ai_usage

Revision ID: 6b0c386a248a
Revises: a1b2c3d4e5f6
Create Date: 2026-08-21 17:10:55.486285
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '6b0c386a248a'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('ai_usage',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('day', sa.DateTime(timezone=True), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('requests', sa.BigInteger(), nullable=False),
    sa.Column('prompt_tokens', sa.BigInteger(), nullable=False),
    sa.Column('completion_tokens', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('day', 'model', name='uq_ai_usage_day_model')
    )
    op.create_table('classification',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('prompt_version', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(length=32), nullable=True),
    sa.Column('requires_response', sa.Boolean(), nullable=True),
    sa.Column('is_substantive', sa.Boolean(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['message_id'], ['message.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('message_id', 'model', 'prompt_version', name='uq_classification_msg')
    )
    op.create_index('ix_classification_message', 'classification', ['message_id'], unique=False)
    op.create_table('interaction',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chat_id', sa.Integer(), nullable=False),
    sa.Column('thread_id', sa.BigInteger(), nullable=True),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('opened_by_message_id', sa.Integer(), nullable=False),
    sa.Column('last_client_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('client_messages', sa.Integer(), nullable=False),
    sa.Column('first_reaction_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_reaction_staff_id', sa.Integer(), nullable=True),
    sa.Column('substantive_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('substantive_staff_id', sa.Integer(), nullable=True),
    sa.Column('state', sa.Enum('OPEN', 'REACTED', 'ANSWERED', 'NO_RESPONSE_NEEDED', 'ABANDONED', name='interaction_state'), nullable=False),
    sa.Column('ttfr_seconds', sa.BigInteger(), nullable=True),
    sa.Column('ttfr_business_seconds', sa.BigInteger(), nullable=True),
    sa.Column('ttfa_seconds', sa.BigInteger(), nullable=True),
    sa.Column('ttfa_business_seconds', sa.BigInteger(), nullable=True),
    sa.Column('sla_breached', sa.Boolean(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['chat.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['first_reaction_staff_id'], ['staff.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['opened_by_message_id'], ['message.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['substantive_staff_id'], ['staff.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_interaction_active', 'interaction', ['state'], unique=False, postgresql_where=sa.text("state IN ('OPEN', 'REACTED')"))
    op.create_index('ix_interaction_chat_opened', 'interaction', ['chat_id', 'thread_id', 'opened_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_interaction_chat_opened', table_name='interaction')
    op.drop_index('ix_interaction_active', table_name='interaction', postgresql_where=sa.text("state IN ('OPEN', 'REACTED')"))
    op.drop_table('interaction')
    op.drop_index('ix_classification_message', table_name='classification')
    op.drop_table('classification')
    op.drop_table('ai_usage')
