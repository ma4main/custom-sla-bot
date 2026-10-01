"""Обращение, где ни одно сообщение клиента ответа не требует, закрывается
«ответа не требовалось» без реакции, ttfr и просрочки. Фото без вердикта —
вопрос, кроме фото в ответ на «пришлите» при правилах v2."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

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

# Понедельник, 16:22 МСК — рабочее время при календаре по умолчанию.
T0 = datetime(2026, 9, 7, 13, 22, tzinfo=timezone.utc)


async def _chat(session, tg_chat_id: int) -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title="Зенит", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=T0 - timedelta(days=1))
    return chat


async def _say(
    session, chat: Chat, n: int, side: BusinessSide, minutes: int, text: str,
    *, label: str | None, requires: bool | None = None, substantive: bool | None = None,
) -> Message:
    message = Message(
        chat_id=chat.id,
        tg_message_id=n,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=side,
        text=text,
        char_count=len(text),
        # Пустой текст в этих тестах означает фото без подписи.
        media_kind=None if text else "photo",
        has_media=not text,
        sent_at=T0 + timedelta(minutes=minutes),
    )
    session.add(message)
    await session.flush()
    if label is not None:
        session.add(
            Classification(
                message_id=message.id,
                model=get_settings().ai_model,
                prompt_version=9,
                source="model",
                label=label,
                requires_response=requires,
                is_substantive=substantive,
            )
        )
        await session.flush()
    return message


async def _episodes(session, chat: Chat) -> list[Interaction]:
    return list(
        (
            await session.scalars(
                select(Interaction).where(Interaction.chat_id == chat.id).order_by(Interaction.opened_at)
            )
        ).all()
    )


# Следующее утро, 08:27 МСК — до начала рабочего дня.
NEXT_MORNING = 16 * 60 + 5


@requires_db
async def test_thanks_answered_next_morning_is_not_a_breach(session, monkeypatch):
    """«Спасибо 🤗» в 16:22, «ПП в банке» в 08:27 — ответа не требовалось."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _chat(session, -100777601)
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "Спасибо 🤗", label="ack", requires=False)
    await _say(
        session, chat, 2, BusinessSide.COMPANY, NEXT_MORNING, "Доброе утро, в банке ПП",
        label="substantive", substantive=True,
    )

    await rebuild_interactions(session, now=T0 + timedelta(hours=20))
    await session.flush()

    (episode,) = await _episodes(session, chat)
    assert episode.state is InteractionState.NO_RESPONSE_NEEDED
    assert episode.first_reaction_at is None, "реплика компании — не реакция на «спасибо»"
    assert episode.sla_breached is None
    assert episode.ttfr_business_seconds is None
    assert episode.substantive_at is None


@requires_db
async def test_forwarded_file_without_question_is_not_a_breach(session, monkeypatch):
    """Пересланный файл (info, false) — тоже не обращение."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _chat(session, -100777602)
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "делится файлом", label="info", requires=False)
    await _say(session, chat, 2, BusinessSide.CLIENT, 1, "это для сведения", label="info", requires=False)
    await _say(
        session, chat, 3, BusinessSide.COMPANY, 120, "Информация об оплате обслуживания",
        label="substantive", substantive=True,
    )

    await rebuild_interactions(session, now=T0 + timedelta(hours=3))
    await session.flush()

    (episode,) = await _episodes(session, chat)
    assert episode.state is InteractionState.NO_RESPONSE_NEEDED
    assert episode.sla_breached is None
    assert episode.client_messages == 2


@requires_db
@pytest.mark.parametrize("rules_version", [1, 2])
async def test_unprompted_photo_without_verdict_counts_as_question(session, monkeypatch, rules_version):
    """Непрошеное фото без вердикта — вопрос по умолчанию в обеих версиях
    правил: реакция и просрочка считаются."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    monkeypatch.setattr(
        get_settings(), "episode_rules_v2_since",
        T0 - timedelta(days=1) if rules_version == 2 else None, raising=False,
    )
    chat = await _chat(session, -100777603)
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "", label=None)
    await _say(
        session, chat, 2, BusinessSide.COMPANY, NEXT_MORNING, "ПП в банке",
        label="substantive", substantive=True,
    )

    await rebuild_interactions(session, now=T0 + timedelta(hours=20))
    await session.flush()

    (episode,) = await _episodes(session, chat)
    assert episode.version == rules_version
    assert episode.state is InteractionState.ANSWERED
    assert episode.first_reaction_at is not None
    assert episode.sla_breached is True


@requires_db
@pytest.mark.parametrize("rules_version", [1, 2])
async def test_requested_photo_without_verdict_is_an_answer_in_v2(session, monkeypatch, rules_version):
    """Фото в ответ на «пришлите скан» — ответ клиента без таймера (правила v2).
    В v1 такое фото открывало новое обращение и давало ложную просрочку."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    monkeypatch.setattr(
        get_settings(), "episode_rules_v2_since",
        T0 - timedelta(days=1) if rules_version == 2 else None, raising=False,
    )
    chat = await _chat(session, -100777605)
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "Подпишите договор", label="request", requires=True)
    await _say(session, chat, 2, BusinessSide.COMPANY, 1, "Подписала", label="substantive", substantive=True)
    await _say(
        session, chat, 3, BusinessSide.COMPANY, 2, "Пришлите, пожалуйста, скан паспорта",
        label="question", substantive=False,
    )
    photo = await _say(session, chat, 4, BusinessSide.CLIENT, 5, "", label=None)
    await _say(
        session, chat, 5, BusinessSide.COMPANY, NEXT_MORNING, "ПП в банке",
        label="substantive", substantive=True,
    )

    await rebuild_interactions(session, now=T0 + timedelta(hours=20))
    await session.flush()

    by_opener = {episode.opened_by_message_id: episode for episode in await _episodes(session, chat)}
    if rules_version == 2:
        assert by_opener[photo.id].state is InteractionState.NO_RESPONSE_NEEDED
        assert by_opener[photo.id].sla_breached is None
    else:
        assert by_opener[photo.id].sla_breached is True


@requires_db
async def test_thanks_then_real_request_keeps_reaction_on_the_request(session, monkeypatch):
    """«Спасибо», следом просьба, потом ответ компании: реакция — у просьбы.

    Серия «спасибо» режется существующим правилом (она не наследует срок),
    просьба открывает своё обращение и получает реакцию как обычно.
    """
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _chat(session, -100777604)
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "Спасибо", label="ack", requires=False)
    request = await _say(session, chat, 2, BusinessSide.CLIENT, 1, "И ещё счёт", label="request", requires=True)
    await _say(session, chat, 3, BusinessSide.COMPANY, 3, "принято", label="ack", substantive=False)

    await rebuild_interactions(session, now=T0 + timedelta(hours=1))
    await session.flush()

    thanks, asked = await _episodes(session, chat)
    assert thanks.state is InteractionState.NO_RESPONSE_NEEDED
    assert thanks.first_reaction_at is None
    assert asked.opened_by_message_id == request.id
    assert asked.first_reaction_at is not None
    assert asked.sla_breached is False
