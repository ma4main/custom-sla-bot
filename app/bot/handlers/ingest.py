"""Приём групповых сообщений. В группах бот молчит всегда (docs/SCREENS.md,
раздел 0): обработчики только сохраняют."""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from app.db.base import session_scope
from app.services.ingestion import ingest_message

log = structlog.get_logger(__name__)

router = Router(name="ingest")

# Только групповые чаты. Приватные диалоги обрабатывает отдельный роутер.
_IN_GROUP = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
router.message.filter(_IN_GROUP)
router.edited_message.filter(_IN_GROUP)


def _dump(message: Message) -> dict:
    # by_alias=True возвращает поле отправителя под именем "from", как в Bot API.
    return message.model_dump(mode="python", by_alias=True, exclude_none=True)


@router.message()
async def on_group_message(message: Message) -> None:
    async with session_scope() as session:
        await ingest_message(session, _dump(message))


@router.edited_message()
async def on_group_message_edited(message: Message) -> None:
    async with session_scope() as session:
        await ingest_message(session, _dump(message), is_edit=True)
