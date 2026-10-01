"""Начальная схема

Revision ID: 4dce9babaa2e
Revises: 
Create Date: 2026-08-19 17:48:34.694436
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '4dce9babaa2e'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('chat',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tg_chat_id', sa.BigInteger(), nullable=False),
    sa.Column('title', sa.String(length=512), nullable=True),
    sa.Column('state', sa.Enum('DISCOVERED', 'TRACKED', 'PAUSED', 'ARCHIVED', name='chat_state'), nullable=False),
    sa.Column('is_forum', sa.Boolean(), nullable=False),
    sa.Column('client_name', sa.String(length=255), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=True),
    sa.Column('tracked_since', sa.DateTime(timezone=True), nullable=True),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('settings', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tg_chat_id')
    )
    op.create_table('staff',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('full_name', sa.String(length=255), nullable=False),
    sa.Column('normalized_name', sa.String(length=255), nullable=False),
    sa.Column('discriminator', sa.String(length=255), nullable=True),
    sa.Column('aliases', postgresql.ARRAY(sa.String()), nullable=False),
    sa.Column('tg_user_id', sa.BigInteger(), nullable=True),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
    sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('normalized_name', 'discriminator', name='uq_staff_name_disc'),
    sa.UniqueConstraint('tg_user_id')
    )
    op.create_table('telegram_update',
    sa.Column('update_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('update_type', sa.String(length=64), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processing_error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('update_id')
    )
    op.create_index('ix_tg_update_unprocessed', 'telegram_update', ['received_at'], unique=False, postgresql_where=sa.text('processed_at IS NULL'))
    op.create_table('bot_user',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tg_user_id', sa.BigInteger(), nullable=False),
    sa.Column('username', sa.String(length=255), nullable=True),
    sa.Column('display_name', sa.String(length=255), nullable=True),
    sa.Column('role', sa.Enum('OWNER', 'ADMIN', 'MANAGER', 'MAINTAINER', name='bot_role'), nullable=False),
    sa.Column('permissions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('state', sa.Enum('PENDING', 'ACTIVE', 'DISABLED', name='bot_user_state'), nullable=False),
    sa.Column('staff_id', sa.Integer(), nullable=True),
    sa.Column('invited_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['invited_by'], ['bot_user.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['staff_id'], ['staff.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tg_user_id')
    )
    op.create_table('chat_title_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chat_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=512), nullable=True),
    sa.Column('changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['chat.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('message',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chat_id', sa.Integer(), nullable=False),
    sa.Column('thread_id', sa.BigInteger(), nullable=True),
    sa.Column('tg_message_id', sa.BigInteger(), nullable=False),
    sa.Column('tg_user_id', sa.BigInteger(), nullable=True),
    sa.Column('transport_actor_kind', sa.Enum('HUMAN_USER', 'INTEGRATOR_BOT', 'OTHER_BOT', 'TELEGRAM_SYSTEM', name='transport_actor_kind'), nullable=False),
    sa.Column('business_side', sa.Enum('CLIENT', 'COMPANY', 'INTEGRATOR_SYSTEM', 'UNKNOWN', name='business_side'), nullable=False),
    sa.Column('side_rule_version', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=True),
    sa.Column('entities', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('char_count', sa.Integer(), nullable=False),
    sa.Column('media_kind', sa.String(length=32), nullable=True),
    sa.Column('has_media', sa.Boolean(), nullable=False),
    sa.Column('reply_to_tg_message_id', sa.BigInteger(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('needs_reclassification', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['chat.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('chat_id', 'tg_message_id', name='uq_message_chat_tgid')
    )
    op.create_index('ix_message_chat_thread_sent', 'message', ['chat_id', 'thread_id', 'sent_at'], unique=False)
    op.create_index('ix_message_needs_reclass', 'message', ['chat_id'], unique=False, postgresql_where=sa.text('needs_reclassification'))
    op.create_index('ix_message_side_sent', 'message', ['business_side', 'sent_at'], unique=False)
    op.create_table('attribution',
    sa.Column('message_id', sa.Integer(), nullable=False),
    sa.Column('staff_id', sa.Integer(), nullable=True),
    sa.Column('method', sa.Enum('EXACT', 'TG_ID', 'FUZZY', 'AI', 'CONTINUATION', 'MANUAL', name='attribution_method'), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('parser_version', sa.Integer(), nullable=False),
    sa.Column('raw_name', sa.String(length=255), nullable=True),
    sa.Column('assigned_by', sa.Integer(), nullable=True),
    sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['assigned_by'], ['bot_user.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['message_id'], ['message.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['staff_id'], ['staff.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('message_id')
    )
    op.create_index('ix_attribution_staff', 'attribution', ['staff_id'], unique=False, postgresql_where=sa.text('staff_id IS NOT NULL'))
    op.create_index('ix_attribution_unresolved', 'attribution', ['parser_version'], unique=False, postgresql_where=sa.text('staff_id IS NULL'))
    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('actor_user_id', sa.Integer(), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('object_type', sa.String(length=64), nullable=True),
    sa.Column('object_id', sa.String(length=64), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['bot_user.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_created', 'audit_log', ['created_at'], unique=False)
    op.create_table('invite_code',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('role', sa.Enum('OWNER', 'ADMIN', 'MANAGER', 'MAINTAINER', name='bot_role'), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('used_by', sa.Integer(), nullable=True),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['bot_user.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['used_by'], ['bot_user.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('setting',
    sa.Column('key', sa.String(length=128), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('updated_by', sa.Integer(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['updated_by'], ['bot_user.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('key')
    )


def downgrade() -> None:
    op.drop_table('setting')
    op.drop_table('invite_code')
    op.drop_index('ix_audit_created', table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index('ix_attribution_unresolved', table_name='attribution', postgresql_where=sa.text('staff_id IS NULL'))
    op.drop_index('ix_attribution_staff', table_name='attribution', postgresql_where=sa.text('staff_id IS NOT NULL'))
    op.drop_table('attribution')
    op.drop_index('ix_message_side_sent', table_name='message')
    op.drop_index('ix_message_needs_reclass', table_name='message', postgresql_where=sa.text('needs_reclassification'))
    op.drop_index('ix_message_chat_thread_sent', table_name='message')
    op.drop_table('message')
    op.drop_table('chat_title_history')
    op.drop_table('bot_user')
    op.drop_index('ix_tg_update_unprocessed', table_name='telegram_update', postgresql_where=sa.text('processed_at IS NULL'))
    op.drop_table('telegram_update')
    op.drop_table('staff')
    op.drop_table('chat')
