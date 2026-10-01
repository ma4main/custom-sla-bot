"""Удаление чата, добавленного по ошибке.

Удаление необратимо, поэтому у него две страховки: разрешено только для
архивных чатов, и сырой журнал апдейтов при этом не трогается.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db.models import (
    AuditLog,
    BotRole,
    BotUser,
    BotUserState,
    BusinessSide,
    Chat,
    ChatState,
    Message,
    TelegramUpdate,
    TransportActorKind,
)
from app.bot.callbacks import ChatAction
from app.bot.keyboards import chat_card
from app.services.chats import ChatError, delete_chat, set_state
from app.services.tracking import open_period
from tests.conftest import requires_db

NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


async def _owner(session) -> BotUser:
    actor = BotUser(
        tg_user_id=777001, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
    )
    session.add(actor)
    await session.flush()
    return actor


async def _chat(session, state: ChatState, tg_chat_id: int = -100666001) -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title="Ошибочный чат", state=state)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))

    session.add(
        Message(
            chat_id=chat.id,
            tg_message_id=1,
            transport_actor_kind=TransportActorKind.HUMAN_USER,
            business_side=BusinessSide.CLIENT,
            text="Случайное сообщение",
            char_count=19,
            sent_at=NOW,
        )
    )
    await session.flush()
    return chat


@requires_db
async def test_archived_chat_is_deleted_with_its_messages(session):
    actor = await _owner(session)
    chat = await _chat(session, ChatState.ARCHIVED)
    chat_id = chat.id

    summary = await delete_chat(session, actor, chat)
    await session.flush()

    assert summary["messages"] == 1
    assert await session.get(Chat, chat_id) is None
    left = await session.scalar(
        select(func.count(Message.id)).where(Message.chat_id == chat_id)
    )
    assert left == 0, "сообщения удалённого чата остались висеть"


@requires_db
async def test_tracked_chat_cannot_be_deleted(session):
    """Живой чат удалить нельзя: это молча унесло бы работающие данные."""
    actor = await _owner(session)
    chat = await _chat(session, ChatState.TRACKED, tg_chat_id=-100666002)

    with pytest.raises(ChatError, match="архива"):
        await delete_chat(session, actor, chat)

    assert await session.get(Chat, chat.id) is not None


@requires_db
async def test_raw_update_journal_survives_deletion(session):
    """Удаляется интерпретация, а не истина.

    Журнал апдейтов не привязан к чату и обязан пережить удаление — иначе
    сырьё перестало бы быть неизменным (docs/ARCHITECTURE.md).
    """
    actor = await _owner(session)
    chat = await _chat(session, ChatState.ARCHIVED, tg_chat_id=-100666003)
    session.add(
        TelegramUpdate(
            update_id=987654321,
            update_type="message",
            payload={"message": {"chat": {"id": chat.tg_chat_id}, "text": "привет"}},
        )
    )
    await session.flush()

    await delete_chat(session, actor, chat)
    await session.flush()

    survived = await session.get(TelegramUpdate, 987654321)
    assert survived is not None, "удаление чата унесло сырой журнал"


@requires_db
async def test_deletion_leaves_a_trace_in_the_audit_log(session):
    """После удаления не останется ни чата, ни названия — след обязан быть."""
    actor = await _owner(session)
    chat = await _chat(session, ChatState.ARCHIVED, tg_chat_id=-100666004)

    await delete_chat(session, actor, chat)
    await session.flush()

    entry = await session.scalar(
        select(AuditLog).where(AuditLog.action == "chat.deleted")
    )
    assert entry is not None
    assert entry.payload["title"] == "Ошибочный чат"
    assert entry.payload["messages"] == 1


# ═══════════════════════════════════════════════════════════════
# Переходы состояний: путь в архив руками
# ═══════════════════════════════════════════════════════════════


@requires_db
async def test_chat_can_be_archived_and_returned_by_hand(session):
    """Чат уходит в архив кнопкой, а не только когда бота выгнали из группы."""
    actor = await _owner(session)
    chat = await _chat(session, ChatState.TRACKED, tg_chat_id=-100666010)

    await set_state(session, actor, chat, ChatState.ARCHIVED)
    await session.flush()

    assert chat.state is ChatState.ARCHIVED
    assert chat.archived_at is not None
    # Из архива удаление разрешено — путь «лишний чат → совсем убрать» замкнут.
    await delete_chat(session, actor, chat)


@requires_db
async def test_returning_from_archive_clears_the_archive_mark(session):
    actor = await _owner(session)
    chat = await _chat(session, ChatState.ARCHIVED, tg_chat_id=-100666011)

    await set_state(session, actor, chat, ChatState.TRACKED)
    await session.flush()

    assert chat.state is ChatState.TRACKED
    assert chat.archived_at is None, "чат вернули в анализ, а отметка архива осталась"


@requires_db
async def test_chat_card_offers_a_way_out_of_every_state(session):
    """Из любого состояния есть путь в остальные — по фактическим кнопкам карточки."""
    chat = await _chat(session, ChatState.TRACKED, tg_chat_id=-100666012)

    def actions(state: ChatState) -> set[str]:
        chat.state = state
        markup = chat_card(chat, page=0)
        return {
            ChatAction.unpack(button.callback_data).action
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data and button.callback_data.startswith("chat:")
        }

    assert {"pause", "archive"} <= actions(ChatState.TRACKED)
    assert {"track", "archive"} <= actions(ChatState.PAUSED)
    assert {"track", "delete"} <= actions(ChatState.ARCHIVED)
    assert "archive" not in actions(ChatState.ARCHIVED), "архивный чат нельзя архивировать"
