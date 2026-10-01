"""Интервалы наблюдения: история отчётов не зависит от сегодняшнего состояния."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    ChatTrackingPeriod,
    Message,
    TransportActorKind,
)
from app.services.tracking import backfill, close_period, observed_filter, open_period
from tests.conftest import requires_db

JULY = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)


def client_message(chat_id: int, tg_id: int, sent_at: datetime) -> Message:
    return Message(
        chat_id=chat_id,
        tg_message_id=tg_id,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Добрый день",
        char_count=11,
        sent_at=sent_at,
    )


@requires_db
async def test_messages_outside_observation_are_not_counted(session):
    chat = Chat(tg_chat_id=-100999101, title="С паузой", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()

    # Наблюдали июль, затем пауза, затем снова наблюдаем с августа.
    session.add_all(
        [
            ChatTrackingPeriod(
                chat_id=chat.id, started_at=JULY - timedelta(days=14), ended_at=JULY
            ),
            ChatTrackingPeriod(chat_id=chat.id, started_at=JULY + timedelta(days=14)),
        ]
    )
    session.add_all(
        [
            client_message(chat.id, 1, JULY - timedelta(days=1)),   # наблюдали
            client_message(chat.id, 2, JULY + timedelta(days=3)),   # пауза
            client_message(chat.id, 3, JULY + timedelta(days=20)),  # снова наблюдаем
        ]
    )
    await session.flush()

    counted = await session.scalar(
        select(func.count(Message.id))
        .where(Message.chat_id == chat.id)
        .where(observed_filter())
    )
    assert counted == 2, "сообщения из паузы попали в отчёт"


@requires_db
async def test_archived_chat_keeps_its_history(session):
    """Архив закрывает интервал, но прошлое из отчётов не исчезает."""
    chat = Chat(tg_chat_id=-100999102, title="Архивный", state=ChatState.ARCHIVED)
    session.add(chat)
    await session.flush()

    await open_period(session, chat, reason="test", at=JULY - timedelta(days=10))
    session.add(client_message(chat.id, 1, JULY - timedelta(days=5)))
    await session.flush()
    await close_period(session, chat, reason="archived", at=JULY)

    counted = await session.scalar(
        select(func.count(Message.id))
        .where(Message.chat_id == chat.id)
        .where(observed_filter())
    )
    assert counted == 1


@requires_db
async def test_open_period_is_not_duplicated(session):
    chat = Chat(tg_chat_id=-100999103, title="Повторное включение", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()

    await open_period(session, chat, reason="first")
    await open_period(session, chat, reason="second")
    await session.flush()

    open_count = await session.scalar(
        select(func.count(ChatTrackingPeriod.id))
        .where(ChatTrackingPeriod.chat_id == chat.id)
        .where(ChatTrackingPeriod.ended_at.is_(None))
    )
    assert open_count == 1


@requires_db
async def test_backfill_skips_chats_that_were_never_tracked(session):
    session.add(
        Chat(tg_chat_id=-100999104, title="Только обнаружен", state=ChatState.DISCOVERED)
    )
    await session.flush()

    await backfill(session)
    await session.flush()

    periods = await session.scalar(
        select(func.count(ChatTrackingPeriod.id)).join(Chat, Chat.id == ChatTrackingPeriod.chat_id)
        .where(Chat.tg_chat_id == -100999104)
    )
    assert periods == 0
