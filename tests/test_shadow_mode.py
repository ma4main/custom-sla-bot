"""Тихий режим ИИ проверяется поведением на одних и тех же данных."""

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
from app.services.verdicts import SOURCE_MODEL
from tests.conftest import requires_db

NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


async def _chat_with_info_message(session):
    """Клиент прислал то, что модель считает не требующим ответа."""
    chat = Chat(tg_chat_id=-100777001, title="Тихий режим", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))

    message = Message(
        chat_id=chat.id,
        tg_message_id=1,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Счёт оплатили, спасибо за оперативность",
        char_count=39,
        sent_at=NOW - timedelta(hours=2),
    )
    session.add(message)
    await session.flush()

    session.add(
        Classification(
            message_id=message.id,
            model=get_settings().ai_model,
            prompt_version=1,
            source=SOURCE_MODEL,
            label="info",
            requires_response=False,
        )
    )
    await session.flush()
    return chat


async def _states(session, chat_id: int) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Interaction.state, func.count())
            .where(Interaction.chat_id == chat_id)
            .group_by(Interaction.state)
        )
    ).all()
    return {state.value: count for state, count in rows}


@requires_db
async def test_model_verdict_closes_episode_when_active(session, monkeypatch):
    chat = await _chat_with_info_message(session)
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_shadow_mode", False, raising=False)

    # now пинуется к NOW: иначе фикстура с фиксированными датами со временем
    # перешагнёт предел длительности эпизода и тест начнёт падать сам по себе.
    await rebuild_interactions(session, now=NOW)
    await session.flush()

    assert _states and (await _states(session, chat.id)).get("no_response_needed") == 1


@requires_db
async def test_model_verdict_is_ignored_in_shadow_mode(session, monkeypatch):
    """В тихом режиме вердикт модели на эпизоды не влияет — обращение живо."""
    chat = await _chat_with_info_message(session)
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_shadow_mode", True, raising=False)

    # now пинуется к NOW: иначе фикстура с фиксированными датами со временем
    # перешагнёт предел длительности эпизода и тест начнёт падать сам по себе.
    await rebuild_interactions(session, now=NOW)
    await session.flush()

    states = await _states(session, chat.id)
    assert states.get("no_response_needed") is None
    assert states.get("open") == 1
