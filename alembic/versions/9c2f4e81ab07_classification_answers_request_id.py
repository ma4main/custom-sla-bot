"""classification.answers_request_id: какое обращение закрывает ответ

Revision ID: 9c2f4e81ab07
Revises: f3c81b6d9a24
Create Date: 2026-09-14 21:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9c2f4e81ab07"
down_revision: Union[str, None] = "f3c81b6d9a24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "classification",
        sa.Column("answers_request_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("classification", "answers_request_id")
