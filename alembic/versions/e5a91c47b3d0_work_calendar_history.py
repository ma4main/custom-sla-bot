"""work_calendar: одноразовое преобразование формата настройки календаря в историю

Revision ID: e5a91c47b3d0
Revises: d1f7a3b5c802
Create Date: 2026-09-03 15:00:00
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5a91c47b3d0'
down_revision: Union[str, None] = 'd1f7a3b5c802'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULTS = {
    'weekdays': [1, 2, 3, 4, 5],
    'start': '10:00',
    'end': '19:00',
    'timezone': 'Europe/Moscow',
    'holidays': [],
}


def upgrade() -> None:
    connection = op.get_bind()
    row = connection.execute(
        sa.text("SELECT value FROM setting WHERE key = 'work_calendar'")
    ).first()
    if row is None:
        return

    value = dict(row[0] or {})
    if value.get('history'):
        return

    # Текущее значение становится единственной записью истории, действующей всегда.
    previous = {key: value.get(key, default) for key, default in DEFAULTS.items()}
    previous['since'] = None

    value['history'] = [previous]
    value.setdefault('since', None)

    connection.execute(
        sa.text(
            "UPDATE setting SET value = CAST(:value AS jsonb) "
            "WHERE key = 'work_calendar'"
        ),
        {'value': json.dumps(value)},
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE setting SET value = value - 'history' - 'since' "
            "WHERE key = 'work_calendar'"
        )
    )
