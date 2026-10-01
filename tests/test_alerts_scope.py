"""Кого алерты будят, а кого нет: проверяется поведение, а не наличие строк
в коде.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    ChatTrackingPeriod,
    Interaction,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.tracking import close_period, currently_tracked_chats, open_period
from tests.conftest import requires_db

NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


async def _chat_with_open_episode(session, tg_id: int, state: ChatState) -> Chat:
    chat = Chat(tg_chat_id=tg_id, title=f"Чат {tg_id}", state=state)
    session.add(chat)
    await session.flush()

    message = Message(
        chat_id=chat.id,
        tg_message_id=1,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Когда будет акт?",
        char_count=16,
        sent_at=NOW - timedelta(hours=3),
    )
    session.add(message)
    await session.flush()

    session.add(
        Interaction(
            chat_id=chat.id,
            opened_at=message.sent_at,
            opened_by_message_id=message.id,
            last_client_at=message.sent_at,
            client_messages=1,
            state=InteractionState.OPEN,
        )
    )
    await session.flush()
    return chat


async def _waiting_count(session) -> int:
    return await session.scalar(
        select(func.count(Interaction.id))
        .where(Interaction.state.in_([InteractionState.OPEN, InteractionState.REACTED]))
        .where(Interaction.chat_id.in_(currently_tracked_chats()))
    )


@requires_db
async def test_tracked_chat_is_waiting(session):
    chat = await _chat_with_open_episode(session, -100888001, ChatState.TRACKED)
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))
    await session.flush()

    assert await _waiting_count(session) == 1


@requires_db
async def test_paused_chat_stops_waiting(session):
    """Пауза чата обязана останавливать и алерты по нему."""
    chat = await _chat_with_open_episode(session, -100888002, ChatState.TRACKED)
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))
    await session.flush()
    assert await _waiting_count(session) == 1

    chat.state = ChatState.PAUSED
    await close_period(session, chat, reason="paused")
    await session.flush()

    assert await _waiting_count(session) == 0, "по чату на паузе всё ещё придёт алерт"


@requires_db
async def test_archived_chat_stops_waiting(session):
    chat = await _chat_with_open_episode(session, -100888003, ChatState.ARCHIVED)
    session.add(
        ChatTrackingPeriod(
            chat_id=chat.id,
            started_at=NOW - timedelta(days=2),
            ended_at=NOW - timedelta(hours=1),
        )
    )
    await session.flush()

    assert await _waiting_count(session) == 0
