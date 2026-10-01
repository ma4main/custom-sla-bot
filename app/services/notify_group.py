"""Переезд группы уведомлений (миграция Telegram в супергруппу) без правки конфига.

Новый номер ловится тремя путями: сообщение о миграции в самой группе (бот),
ошибка TelegramMigrateToChat при отправке (воркер досылает в новый номер),
чтение сохранённого переезда на старте обоих процессов.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, set_notify_group_override
from app.db.models import AuditLog, Setting

log = structlog.get_logger(__name__)

# Служебное состояние, не настройка человека: ключ setting вне DEFAULTS.
STATE_KEY = "notify_group_runtime"


async def load_migration(session: AsyncSession) -> int | None:
    stored = await session.get(Setting, STATE_KEY)
    if stored is None or not isinstance(stored.value, dict):
        return None
    migrated = stored.value.get("migrated_to")
    if migrated:
        set_notify_group_override(int(migrated))
        return int(migrated)
    return None


async def record_migration(session: AsyncSession, new_chat_id: int) -> bool:
    """Запомнить переезд и сразу применить к процессу. False — уже записан этот номер.
    Второй процесс подхватит на старте (load_migration).
    """
    stored = await session.get(Setting, STATE_KEY)
    already = (
        stored.value.get("migrated_to")
        if stored is not None and isinstance(stored.value, dict)
        else None
    )
    set_notify_group_override(new_chat_id)
    if already == new_chat_id:
        return False

    if stored is None:
        session.add(Setting(key=STATE_KEY, value={"migrated_to": new_chat_id}))
    else:
        stored.value = {**(stored.value or {}), "migrated_to": new_chat_id}
    session.add(
        AuditLog(
            actor_user_id=None,
            action="notify_group.migrated",
            object_type="setting",
            object_id=STATE_KEY,
            payload={
                "from": get_settings().notify_group_chat_id,
                "to": new_chat_id,
            },
        )
    )
    log.warning(
        "notify_group.migrated",
        old=get_settings().notify_group_chat_id,
        new=new_chat_id,
    )
    return True


MIGRATION_NOTICE = (
    "ℹ️ <b>Группа уведомлений переехала</b>\n\n"
    "Telegram преобразовал её в супергруппу и сменил внутренний номер. "
    "Бот перестроился сам: алерты и отчёты идут в неё дальше, анализ "
    "по ней не ведётся.\n\n"
    "<i>Для порядка стоит при случае обновить NOTIFY_GROUP_CHAT_ID "
    "в конфигурации сервера. Это делает администратор сервера.</i>"
)
