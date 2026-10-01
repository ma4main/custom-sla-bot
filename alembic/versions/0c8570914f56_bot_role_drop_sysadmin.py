"""bot_role: без SYSADMIN и MAINTAINER; такие учётки становятся администраторами

Значения из enum PostgreSQL не удаляются — тип пересоздаётся. Приглашения с этими ролями
тоже переводятся в ADMIN, а непогашенные сразу истекают: иначе они стали бы рабочими
приглашениями администратора. Сравнения идут по role::text: на пустой базе SYSADMIN
добавлен в той же транзакции, и литерал этого значения PostgreSQL не примет.
downgrade возвращает значения типа; кто был системным администратором, восстановить нельзя.

Revision ID: 0c8570914f56
Revises: c7f2a41b8d59
Create Date: 2026-09-27 12:00:00
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0c8570914f56"
down_revision: Union[str, None] = "c7f2a41b8d59"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Журнал действий: по записи на каждую переведённую учётку, до смены роли.
    op.execute(
        """
        INSERT INTO audit_log (actor_user_id, action, object_type, object_id, payload)
        SELECT NULL,
               CASE WHEN role::text = 'SYSADMIN' THEN 'sysadmin.revoked'
                    ELSE 'user.role_changed' END,
               'bot_user',
               id::text,
               jsonb_build_object(
                   'from', lower(role::text),
                   'to', 'admin',
                   'reason', 'роль удалена из системы'
               )
        FROM bot_user
        WHERE role::text IN ('SYSADMIN', 'MAINTAINER')
        ORDER BY id
        """
    )
    op.execute(
        "UPDATE bot_user SET role = 'ADMIN' WHERE role::text IN ('SYSADMIN', 'MAINTAINER')"
    )
    op.execute(
        """
        UPDATE invite_code
        SET role = 'ADMIN',
            expires_at = CASE WHEN used_at IS NULL THEN now() ELSE expires_at END
        WHERE role::text IN ('SYSADMIN', 'MAINTAINER')
        """
    )

    op.execute("ALTER TYPE bot_role RENAME TO bot_role_old")
    op.execute("CREATE TYPE bot_role AS ENUM ('OWNER', 'ADMIN', 'MANAGER')")
    op.execute("ALTER TABLE bot_user ALTER COLUMN role TYPE bot_role USING role::text::bot_role")
    op.execute(
        "ALTER TABLE invite_code ALTER COLUMN role TYPE bot_role USING role::text::bot_role"
    )
    op.execute("DROP TYPE bot_role_old")


def downgrade() -> None:
    # Порядок значений — как до upgrade: MAINTAINER из начальной схемы, SYSADMIN добавлен позже.
    op.execute("ALTER TYPE bot_role ADD VALUE IF NOT EXISTS 'MAINTAINER'")
    op.execute("ALTER TYPE bot_role ADD VALUE IF NOT EXISTS 'SYSADMIN'")
