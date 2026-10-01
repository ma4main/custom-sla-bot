"""Новая просьба после реакции помощника — новое обращение со своим сроком.

Просьба или вопрос (requires_response=true) после реакции без передачи
и без ответа по существу режет обращение: старое закрывается как «помощник
ответил сам», новое ждёт своей реакции.

Что НЕ режет: дополнения к текущей работе («вот сумма» — info, false),
обращение без реакции вовсе (сообщения до первого ответа — одно обращение)
и, в v1/v2, обращение с передачей специалисту (его срок уже идёт); с v3
у новой просьбы после передачи своё ожидание.
"""

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

# Пятница, 11:27 МСК — рабочее время при календаре по умолчанию.
T0 = datetime(2026, 8, 28, 8, 27, tzinfo=timezone.utc)


async def _chat(session, tg_chat_id: int) -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title="ООО Ромашка", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=T0 - timedelta(days=1))
    return chat


async def _say(
    session, chat: Chat, n: int, side: BusinessSide, minutes: int, text: str,
    *, label: str, requires: bool | None = None, substantive: bool | None = None,
) -> Message:
    message = Message(
        chat_id=chat.id,
        tg_message_id=n,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=side,
        text=text,
        char_count=len(text),
        sent_at=T0 + timedelta(minutes=minutes),
    )
    session.add(message)
    await session.flush()
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


@requires_db
async def test_new_request_after_reaction_opens_its_own_episode(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _chat(session, -100777501)
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "не пускает", label="request", requires=True)
    await _say(
        session, chat, 2, BusinessSide.COMPANY, 1, "Написала программисту, жду от него ответа.",
        label="ack", substantive=False,
    )
    await _say(session, chat, 3, BusinessSide.CLIENT, 2, "ок", label="ack", requires=False)
    second = await _say(
        session, chat, 4, BusinessSide.CLIENT, 2,
        "Ирина, передайте Тамаре, чтобы она мне позвонила", label="request", requires=True,
    )

    # Через 40 минут после просьбы реакции на неё ещё нет.
    await rebuild_interactions(session, now=T0 + timedelta(minutes=42))
    await session.flush()

    first, new = await _episodes(session, chat)
    assert first.state is InteractionState.ANSWERED, "старое обращение закрыто помощником"
    assert first.first_reaction_at is not None
    assert new.opened_by_message_id == second.id
    assert new.state is InteractionState.OPEN, "новая просьба ждёт своей реакции"
    assert new.first_reaction_at is None


@requires_db
async def test_addition_to_current_work_does_not_split(session, monkeypatch):
    """«Вот ещё сумма» (info, false) — дополнение, не новое обращение."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _chat(session, -100777502)
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "Сделайте счёт", label="request", requires=True)
    await _say(session, chat, 2, BusinessSide.COMPANY, 1, "принято в работу", label="ack", substantive=False)
    await _say(session, chat, 3, BusinessSide.CLIENT, 3, "Сумма 87500", label="info", requires=False)
    await _say(session, chat, 4, BusinessSide.CLIENT, 4, "Без ндс", label="info", requires=False)

    await rebuild_interactions(session, now=T0 + timedelta(hours=1))
    await session.flush()

    episodes = await _episodes(session, chat)
    assert len(episodes) == 1
    assert episodes[0].client_messages == 3


@requires_db
@pytest.mark.parametrize("rules_version", [1, 2, 3])
async def test_request_after_handoff_stays_with_specialist_layer_until_v3(session, monkeypatch, rules_version):
    """v1/v2: после передачи специалисту срок уже идёт — новая просьба
    приклеивается к переданному обращению. v3: у неё своё ожидание,
    переданное продолжает ждать специалиста."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    monkeypatch.setattr(
        get_settings(), "episode_rules_v2_since",
        T0 - timedelta(days=1) if rules_version >= 2 else None, raising=False,
    )
    monkeypatch.setattr(
        get_settings(), "episode_rules_v3_since",
        T0 - timedelta(days=1) if rules_version == 3 else None, raising=False,
    )
    chat = await _chat(session, -100777503)
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "Как оплатить?", label="question", requires=True)
    await _say(
        session, chat, 2, BusinessSide.COMPANY, 1, "передала запрос бухгалтеру",
        label="handoff", substantive=False,
    )
    second = await _say(
        session, chat, 3, BusinessSide.CLIENT, 2, "И ещё нужна декларация с печатью",
        label="request", requires=True,
    )

    await rebuild_interactions(session, now=T0 + timedelta(hours=1))
    await session.flush()

    episodes = await _episodes(session, chat)
    assert episodes[0].handoff_at is not None
    assert episodes[0].version == rules_version
    if rules_version == 3:
        assert len(episodes) == 2, "v3: у новой просьбы своё ожидание реакции"
        assert episodes[1].opened_by_message_id == second.id
        assert episodes[1].first_reaction_at is None
        assert episodes[0].substantive_at is None, "переданное обращение продолжает ждать специалиста"
    else:
        assert len(episodes) == 1
        assert episodes[0].client_messages == 2


@requires_db
@pytest.mark.parametrize("rules_version", [1, 2])
async def test_answer_to_counter_question_does_not_split_even_if_flagged(session, monkeypatch, rules_version):
    """После встречного вопроса компании слово клиента — ответ, а не просьба.

    Модель может ошибиться и пометить ответ как request/true — обращение
    всё равно остаётся одним.
    В v2 то же правило действует и при закрытом обращении
    (tests/test_episode_rules_v2.py).
    """
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    monkeypatch.setattr(
        get_settings(), "episode_rules_v2_since",
        T0 - timedelta(days=1) if rules_version == 2 else None, raising=False,
    )
    chat = await _chat(session, -100777505)
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "Сделайте счёт на аванс", label="request", requires=True)
    await _say(session, chat, 2, BusinessSide.COMPANY, 1, "принято", label="ack", substantive=False)
    await _say(
        session, chat, 3, BusinessSide.COMPANY, 2, "Уточните, запрос от ИП или ошибочно?",
        label="question", substantive=False,
    )
    await _say(session, chat, 4, BusinessSide.CLIENT, 5, "От ИП, по двум договорам", label="request", requires=True)

    await rebuild_interactions(session, now=T0 + timedelta(hours=1))
    await session.flush()

    episodes = await _episodes(session, chat)
    assert len(episodes) == 1, "ответ на встречный вопрос открыл второе обращение — ложный алерт"
    assert episodes[0].client_messages == 2


@requires_db
async def test_messages_before_any_reaction_stay_one_episode(session, monkeypatch):
    """Три просьбы подряд без ответа компании — одно обращение."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _chat(session, -100777504)
    for n, text in enumerate(("Пришлите акт", "И счёт", "И справку"), start=1):
        await _say(session, chat, n, BusinessSide.CLIENT, n, text, label="request", requires=True)

    await rebuild_interactions(session, now=T0 + timedelta(minutes=10))
    await session.flush()

    episodes = await _episodes(session, chat)
    assert len(episodes) == 1
    assert episodes[0].client_messages == 3
