"""Конфликт версий промпта: эпизод строится по ПОСЛЕДНЕЙ версии.

У сообщения могут лежать вердикты обеих версий; без явного порядка выборки
побеждал бы тот, который база отдала последним, — то есть случайный.
"""

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    Classification,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.episodes import rebuild_interactions
from app.services.tracking import open_period
from app.services.verdicts import SOURCE_MODEL
from tests.conftest import requires_db
from tests.test_shadow_mode import _states

NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


async def _message_with_conflicting_versions(
    session, *, v1_requires: bool, v2_requires: bool
) -> Chat:
    chat = Chat(tg_chat_id=-100777002, title="Версии", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))

    message = Message(
        chat_id=chat.id,
        tg_message_id=1,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Мы отправили документы вчера",
        char_count=28,
        sent_at=NOW - timedelta(minutes=10),
    )
    session.add(message)
    await session.flush()

    model = get_settings().ai_model
    for version, requires in ((1, v1_requires), (2, v2_requires)):
        session.add(
            Classification(
                message_id=message.id,
                model=model,
                prompt_version=version,
                source=SOURCE_MODEL,
                label="request" if requires else "info",
                requires_response=requires,
            )
        )
    await session.flush()
    return chat


@requires_db
async def test_v2_no_response_beats_v1_request(session, monkeypatch):
    """v1 «нужен ответ», v2 «не нужен» → обращение закрыто как не требующее."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _message_with_conflicting_versions(
        session, v1_requires=True, v2_requires=False
    )

    await rebuild_interactions(session, now=NOW)
    await session.flush()

    states = await _states(session, chat.id)
    assert states.get(InteractionState.NO_RESPONSE_NEEDED.value) == 1, (
        "победил вердикт СТАРОЙ версии промпта"
    )


@requires_db
async def test_v2_request_beats_v1_no_response(session, monkeypatch):
    """Обратный конфликт: v2 «нужен ответ» — обращение открыто и ждёт."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _message_with_conflicting_versions(
        session, v1_requires=False, v2_requires=True
    )

    await rebuild_interactions(session, now=NOW)
    await session.flush()

    states = await _states(session, chat.id)
    assert states.get(InteractionState.OPEN.value) == 1, (
        "победил вердикт СТАРОЙ версии промпта — обращение потеряно"
    )
