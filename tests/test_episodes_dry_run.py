"""Пересборка эпизодов «в памяти»: боевой движок с подменёнными вердиктами.

1. `persist=False` НЕ ТРОГАЕТ таблицу эпизодов: пересборка может идти
   на боевой базе рядом с живым воркером.
2. `verdict_override` действительно подменяет вердикт.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    Classification,
    Interaction,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.episodes import rebuild_interactions
from app.services.tracking import open_period
from tests.conftest import requires_db

# Понедельник, 10:30 МСК — рабочее время при календаре по умолчанию.
OPENED_AT = datetime(2026, 8, 24, 7, 30, tzinfo=timezone.utc)
NOW = OPENED_AT + timedelta(hours=2)


async def _chat_with_request(session) -> tuple[Chat, Message]:
    chat = Chat(tg_chat_id=-100777301, title="Стенд", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=OPENED_AT - timedelta(days=1))
    message = Message(
        chat_id=chat.id,
        tg_message_id=1,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Сделайте счёт на аванс 150т",
        char_count=27,
        sent_at=OPENED_AT,
    )
    session.add(message)
    await session.flush()
    session.add(
        Classification(
            message_id=message.id,
            model=get_settings().ai_model,
            prompt_version=4,
            source="model",
            label="request",
            requires_response=True,
        )
    )
    await session.flush()
    return chat, message


async def _stored_count(session, chat_id: int) -> int:
    return await session.scalar(
        select(func.count()).select_from(Interaction).where(Interaction.chat_id == chat_id)
    )


@requires_db
async def test_dry_run_leaves_table_untouched_and_returns_items(session):
    """Без записи: таблица как была, эпизоды — в ответе."""
    chat, _ = await _chat_with_request(session)
    await rebuild_interactions(session, now=NOW)
    await session.flush()
    assert await _stored_count(session, chat.id) == 1

    result = await rebuild_interactions(session, now=NOW, persist=False)
    await session.flush()

    ours = [item for item in result["items"] if item.chat_id == chat.id]
    assert len(ours) == 1
    assert ours[0].state is InteractionState.OPEN
    # Боевая таблица не пострадала: ни удаления, ни повторной вставки.
    assert await _stored_count(session, chat.id) == 1


@requires_db
async def test_override_replaces_stored_verdict(session):
    """Подменённый вердикт «ответа не требует» закрывает обращение."""
    chat, message = await _chat_with_request(session)

    as_stored = await rebuild_interactions(session, now=NOW, persist=False)
    [before] = [item for item in as_stored["items"] if item.chat_id == chat.id]
    assert before.state is InteractionState.OPEN

    overridden = await rebuild_interactions(
        session,
        now=NOW,
        persist=False,
        verdict_override={message.id: (False, None, "info")},
    )
    [after] = [item for item in overridden["items"] if item.chat_id == chat.id]
    assert after.state is InteractionState.NO_RESPONSE_NEEDED
