"""Алиасы сотрудников отдельной таблицей и хеш вместо кода приглашения

Revision ID: 5f2a71c0d38e
Revises: 4e7c1a93f2b8
Create Date: 2026-08-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5f2a71c0d38e"
down_revision: Union[str, None] = "4e7c1a93f2b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "staff_alias",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("staff_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["staff_id"], ["staff.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("alias"),
    )
    op.create_index("ix_staff_alias_staff_id", "staff_alias", ["staff_id"])

    op.execute(
        """
        INSERT INTO staff_alias (staff_id, alias)
        SELECT DISTINCT ON (alias) staff_id, alias
        FROM (
            SELECT id AS staff_id, unnest(aliases) AS alias FROM staff
        ) AS flat
        WHERE alias IS NOT NULL AND alias <> ''
        ORDER BY alias, staff_id
        """
    )
    op.drop_column("staff", "aliases")

    op.add_column("invite_code", sa.Column("code_hash", sa.String(length=64), nullable=True))
    op.execute("UPDATE invite_code SET code_hash = encode(sha256(code::bytea), 'hex')")
    op.alter_column("invite_code", "code_hash", nullable=False)
    op.drop_column("invite_code", "code")
    op.create_unique_constraint(None, "invite_code", ["code_hash"])


def downgrade() -> None:
    # Коды приглашений из хеша не восстановить: после отката их выпускают заново.
    op.drop_constraint("invite_code_code_hash_key", "invite_code", type_="unique")
    op.add_column("invite_code", sa.Column("code", sa.String(length=64), nullable=True))
    op.execute("UPDATE invite_code SET code = 'revoked:' || id WHERE code IS NULL")
    op.alter_column("invite_code", "code", nullable=False)
    op.create_unique_constraint(None, "invite_code", ["code"])
    op.drop_column("invite_code", "code_hash")

    op.add_column(
        "staff",
        sa.Column(
            "aliases",
            sa.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
    )
    op.execute(
        """
        UPDATE staff
        SET aliases = COALESCE(sub.list, '{}')
        FROM (
            SELECT staff_id, array_agg(alias ORDER BY id) AS list
            FROM staff_alias GROUP BY staff_id
        ) AS sub
        WHERE staff.id = sub.staff_id
        """
    )
    op.drop_index("ix_staff_alias_staff_id", table_name="staff_alias")
    op.drop_table("staff_alias")
