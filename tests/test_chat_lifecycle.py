"""Жизненный цикл чата: удаление и возврат бота.

Обработчик `on_my_chat_member` зовётся напрямую с настоящим событием aiogram:
ошибку в теле обработчика тест через сервисы не увидел бы.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from aiogram.types import ChatMemberUpdated
from sqlalchemy import select

import app.bot.handlers.chat_lifecycle as lifecycle
from app.db.models import Chat, ChatState, ChatTrackingPeriod
from tests.conftest import requires_db

TG_CHAT_ID = -100888001


def _event(status: str) -> ChatMemberUpdated:
    """Настоящее событие my_chat_member — как его присылает Telegram."""
    member: dict = {
        "user": {"id": 42, "is_bot": True, "first_name": "SLA Monitor"},
        "status": status,
    }
    if status == "kicked":
        member["until_date"] = 0  # Telegram шлёт 0 для «навсегда»
    return ChatMemberUpdated.model_validate(
        {
            "chat": {"id": TG_CHAT_ID, "type": "supergroup", "title": "Лайфцикл"},
            "from": {"id": 1, "is_bot": False, "first_name": "Админ"},
            "date": 1756400000,
            "old_chat_member": {
                "user": {"id": 42, "is_bot": True, "first_name": "SLA Monitor"},
                "status": "member",
            },
            "new_chat_member": member,
        }
    )


@requires_db
async def test_bot_removal_archives_the_chat(session, monkeypatch):
    """Бота выгнали → чат в архиве, интервал наблюдения закрыт."""

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(lifecycle, "session_scope", fake_scope)

    # Чат появляется как при добавлении бота (автовключение по умолчанию).
    await lifecycle.on_my_chat_member(_event("member"))
    await session.flush()
    chat = await session.scalar(select(Chat).where(Chat.tg_chat_id == TG_CHAT_ID))
    assert chat is not None and chat.state is ChatState.TRACKED

    # Бота удалили: чат уходит в архив, а не остаётся TRACKED.
    await lifecycle.on_my_chat_member(_event("left"))
    await session.flush()

    assert chat.state is ChatState.ARCHIVED, "удалённый чат остался в анализе"
    assert chat.archived_at is not None
    open_periods = (
        await session.scalars(
            select(ChatTrackingPeriod)
            .where(ChatTrackingPeriod.chat_id == chat.id)
            .where(ChatTrackingPeriod.ended_at.is_(None))
        )
    ).all()
    assert not open_periods, "интервал наблюдения не закрыт — чат числится живым"


@requires_db
async def test_bot_return_restores_tracking(session, monkeypatch):
    """Бота вернули в группу → чат снова в анализе, интервал открыт."""

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(lifecycle, "session_scope", fake_scope)

    await lifecycle.on_my_chat_member(_event("member"))
    await lifecycle.on_my_chat_member(_event("kicked"))
    await lifecycle.on_my_chat_member(_event("member"))
    await session.flush()

    chat = await session.scalar(select(Chat).where(Chat.tg_chat_id == TG_CHAT_ID))
    assert chat.state is ChatState.TRACKED
    assert chat.archived_at is None
    open_periods = (
        await session.scalars(
            select(ChatTrackingPeriod)
            .where(ChatTrackingPeriod.chat_id == chat.id)
            .where(ChatTrackingPeriod.ended_at.is_(None))
        )
    ).all()
    assert len(open_periods) == 1, "возврат бота не открыл наблюдение заново"


async def test_private_chat_member_update_is_not_a_work_chat(monkeypatch):
    """В личке my_chat_member приходит, когда человек блокирует или разблокирует бота:
    рабочим чатом личка не становится."""
    from unittest.mock import AsyncMock

    from aiogram.dispatcher.event.bases import UNHANDLED

    @asynccontextmanager
    async def fake_scope():
        yield None

    created = AsyncMock(side_effect=AssertionError("личка заведена рабочим чатом"))
    monkeypatch.setattr(lifecycle, "session_scope", fake_scope)
    monkeypatch.setattr(lifecycle, "get_or_create_chat", created)
    private = ChatMemberUpdated.model_validate(
        {
            **_event("kicked").model_dump(mode="json", by_alias=True, exclude_none=True),
            "chat": {"id": 555001, "type": "private", "first_name": "Клиент"},
        }
    )
    result = await lifecycle.router.propagate_event("my_chat_member", private)
    assert result is UNHANDLED
    created.assert_not_awaited()


async def test_group_chat_member_update_reaches_the_handler(monkeypatch):
    from unittest.mock import AsyncMock

    handler = AsyncMock()
    monkeypatch.setattr(
        lifecycle.router.my_chat_member.handlers[0], "callback", handler
    )
    await lifecycle.router.propagate_event("my_chat_member", _event("member"))
    handler.assert_awaited_once()
