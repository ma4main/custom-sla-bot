"""staff: уникальность однофамильцев с NULLS NOT DISTINCT

Revision ID: a1b2c3d4e5f6
Revises: 4dce9babaa2e
Create Date: 2026-08-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "4dce9babaa2e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_staff_name_disc", "staff", type_="unique")
    op.execute(
        "ALTER TABLE staff ADD CONSTRAINT uq_staff_name_disc "
        "UNIQUE NULLS NOT DISTINCT (normalized_name, discriminator)"
    )


def downgrade() -> None:
    op.drop_constraint("uq_staff_name_disc", "staff", type_="unique")
    op.create_unique_constraint(
        "uq_staff_name_disc", "staff", ["normalized_name", "discriminator"]
    )
