"""Жизненный цикл чатов: бот сам замечает, что его добавили в группу или удалили.
Добавленный чат сразу включается в анализ, если включено автовключение
(`chats.auto_track_new`), иначе ждёт в «обнаруженных»."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import ChatMemberUpdated

from app.config import is_notify_group
from app.db.base import session_scope
from app.db.models import ChatState
from app.services.ingestion import get_or_create_chat
from app.services.tracking import close_period, open_period

log = structlog.get_logger(__name__)

router = Router(name="chat_lifecycle")
# Только группы: в личке Telegram присылает то же событие, когда человек блокирует
# или разблокирует бота, — рабочим чатом личка не становится.
GROUP_CHAT_TYPES = frozenset({ChatType.GROUP, ChatType.SUPERGROUP})
router.my_chat_member.filter(F.chat.type.in_(GROUP_CHAT_TYPES))

_PRESENT = {"member", "administrator", "creator"}
_ABSENT = {"left", "kicked"}


@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated) -> None:
    raw_chat = event.chat.model_dump(exclude_none=True)
    new_status = event.new_chat_member.status

    # Группа уведомлений — служебный канал: чат для неё не заводится
    # и наблюдение не открывается (см. app/config.py, is_notify_group).
    if is_notify_group(raw_chat.get("id")):
        log.info("chat.notify_group_ignored", tg_chat_id=raw_chat.get("id"), status=new_status)
        return

    async with session_scope() as session:
        chat = await get_or_create_chat(session, raw_chat)

        if new_status in _ABSENT:
            # История сохраняется: чат попадёт в отчёты за периоды, когда был активен,
            # но из текущих сводок исключается.
            chat.state = ChatState.ARCHIVED
            chat.archived_at = datetime.now(timezone.utc)
            await close_period(session, chat, reason="bot_removed")
            log.info("chat.archived", tg_chat_id=chat.tg_chat_id, status=new_status)
            return

        if new_status in _PRESENT and chat.state is ChatState.ARCHIVED:
            # Бота вернули. При автовключении чат сразу продолжает анализ,
            # история у него и так сохранена.
            from app.services.settings_store import get_value

            auto_track = bool(await get_value(session, "chats", "auto_track_new"))
            chat.state = ChatState.TRACKED if auto_track else ChatState.DISCOVERED
            chat.archived_at = None
            if auto_track and chat.tracked_since is None:
                chat.tracked_since = datetime.now(timezone.utc)
            if auto_track:
                await open_period(session, chat, reason="bot_returned")
            log.info("chat.restored", tg_chat_id=chat.tg_chat_id, auto_tracked=auto_track)
            return

        log.info(
            "chat.membership_changed",
            tg_chat_id=chat.tg_chat_id,
            status=new_status,
            state=chat.state.value,
        )
