"""Правила v3: новая просьба во время ожидания специалиста получает своё
обращение; переданное продолжает ждать специалиста и возобновляется, когда
текущего обращения нет."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.db.models import BusinessSide, InteractionState, Message, TransportActorKind
from app.services.episodes import rebuild_interactions
from tests.conftest import requires_db
from tests.test_episodes_dry_run import OPENED_AT, _chat_with_request
from tests.test_episode_rules_v2 import (
    BOT, CLIENT, COMPANY, T0, VACATION, _chat, _say, _staff,
)


def test_v3_cutover_requires_timezone_and_defaults_off(monkeypatch):
    monkeypatch.delenv("EPISODE_RULES_V3_SINCE", raising=False)
    assert Settings(_env_file=None).episode_rules_v3_since is None
    assert Settings(_env_file=None, EPISODE_RULES_V3_SINCE="").episode_rules_v3_since is None
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EPISODE_RULES_V3_SINCE="2026-09-14T10:00:00")


def _message(chat, n, minute, side, text):
    return Message(
        chat_id=chat.id, tg_message_id=n, business_side=side, text=text, char_count=len(text),
        transport_actor_kind=TransportActorKind.HUMAN_USER, sent_at=OPENED_AT + timedelta(minutes=minute),
    )


@requires_db
@pytest.mark.parametrize("answered", [None, "old", "new"])
async def test_new_request_and_old_handoff_are_independent(session, monkeypatch, answered):
    """Просьба во время ожидания специалиста — своё обращение; ответ компании
    достаётся текущему обращению, а не обоим сразу."""
    monkeypatch.setattr(get_settings(), "episode_rules_v2_since", OPENED_AT, raising=False)
    monkeypatch.setattr(get_settings(), "episode_rules_v3_since", OPENED_AT, raising=False)
    chat, old = await _chat_with_request(session)
    handoff = _message(chat, 2, 1, BusinessSide.COMPANY, "Передала запрос специалисту")
    new = _message(chat, 3, 60, BusinessSide.CLIENT, "Подготовьте другой договор")
    session.add_all([handoff, new])
    await session.flush()
    labels = {old.id: (True, None, "request"), handoff.id: (None, False, "handoff"), new.id: (True, None, "request")}
    if answered == "new":
        reply = _message(chat, 4, 65, BusinessSide.COMPANY, "Договор готов")
        session.add(reply)
        await session.flush()
        labels[reply.id] = (None, True, "substantive")
    elif answered == "old":
        # Новое обращение закрыто, затем специалист отвечает по старому:
        # текущего обращения нет, старое возобновляется и получает ответ.
        done = _message(chat, 4, 65, BusinessSide.COMPANY, "Договор готов")
        later = _message(chat, 5, 70, BusinessSide.COMPANY, "По счёту на аванс: выставлен")
        session.add_all([done, later])
        await session.flush()
        labels[done.id] = (None, True, "substantive")
        labels[later.id] = (None, True, "substantive")

    result = await rebuild_interactions(
        session, now=OPENED_AT + timedelta(hours=3), verdict_override=labels, persist=False
    )
    items = {item.opened_by_message_id: item for item in result["items"] if item.chat_id == chat.id}
    assert set(items) == {old.id, new.id}
    assert items[old.id].version == 3
    assert items[old.id].handoff_at == handoff.sent_at
    assert (items[old.id].substantive_at is not None) == (answered == "old")
    assert (items[new.id].first_reaction_at is not None) == (answered is not None)
    if answered is None:
        assert items[new.id].state is InteractionState.OPEN
        assert items[old.id].state is InteractionState.REACTED

    # Включение v3 после открытия обращения не переписывает его прошлое:
    # обращение, открытое до границы, остаётся одним по правилам v2.
    monkeypatch.setattr(get_settings(), "episode_rules_v3_since", OPENED_AT + timedelta(minutes=2), raising=False)
    prior = await rebuild_interactions(
        session, now=OPENED_AT + timedelta(hours=3), verdict_override=labels, persist=False
    )
    historical = [item for item in prior["items"] if item.chat_id == chat.id]
    assert len(historical) == 1 and historical[0].version == 2


@requires_db
async def test_parked_handoff_keeps_waiting_for_the_specialist(session, monkeypatch):
    """Отложенное обращение не закрывается ответом на новую просьбу и в конце
    остаётся ждать специалиста — по нему возможен алерт «нет ответа»."""
    monkeypatch.setattr(get_settings(), "episode_rules_v2_since", OPENED_AT, raising=False)
    monkeypatch.setattr(get_settings(), "episode_rules_v3_since", OPENED_AT, raising=False)
    chat, old = await _chat_with_request(session)
    handoff = _message(chat, 2, 1, BusinessSide.COMPANY, "Передала запрос бухгалтеру")
    new = _message(chat, 3, 30, BusinessSide.CLIENT, "А мы платили налоги за маркетплейсы?")
    ack = _message(chat, 4, 31, BusinessSide.COMPANY, "передала запрос бухгалтеру")
    session.add_all([handoff, new, ack])
    await session.flush()
    labels = {
        old.id: (True, None, "request"), handoff.id: (None, False, "handoff"),
        new.id: (True, None, "question"), ack.id: (None, False, "handoff"),
    }
    result = await rebuild_interactions(
        session, now=OPENED_AT + timedelta(days=2), verdict_override=labels, persist=False
    )
    items = {item.opened_by_message_id: item for item in result["items"] if item.chat_id == chat.id}
    assert set(items) == {old.id, new.id}
    for item in items.values():
        assert item.handoff_at is not None
        assert item.substantive_at is None
        assert item.state is InteractionState.REACTED, "оба ожидания специалиста живы"


@requires_db
async def test_v2_still_merges_request_into_handoff(session, monkeypatch):
    """Без даты v3 действует прежнее правило: новая просьба приклеивается."""
    monkeypatch.setattr(get_settings(), "episode_rules_v2_since", OPENED_AT, raising=False)
    monkeypatch.setattr(get_settings(), "episode_rules_v3_since", None, raising=False)
    chat, old = await _chat_with_request(session)
    handoff = _message(chat, 2, 1, BusinessSide.COMPANY, "Передала запрос специалисту")
    new = _message(chat, 3, 60, BusinessSide.CLIENT, "Подготовьте другой договор")
    session.add_all([handoff, new])
    await session.flush()
    labels = {old.id: (True, None, "request"), handoff.id: (None, False, "handoff"), new.id: (True, None, "request")}
    result = await rebuild_interactions(
        session, now=OPENED_AT + timedelta(hours=3), verdict_override=labels, persist=False
    )
    items = [item for item in result["items"] if item.chat_id == chat.id]
    assert len(items) == 1 and items[0].version == 2 and items[0].client_messages == 2


@requires_db
async def test_v3_without_v2_date_still_filters_broadcasts(session, monkeypatch):
    """Дата v3 сама включает ВСЕ правила v2, включая фильтр рассылок."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False)
    monkeypatch.setattr(get_settings(), "episode_rules_v2_since", None)
    monkeypatch.setattr(get_settings(), "episode_rules_v3_since", T0)
    chats = [await _chat(session, -100778610 - i) for i in range(3)]
    for i, chat in enumerate(chats):
        await _say(session, chat, 1, CLIENT, 0, "Нужен акт", label="request", requires=True)
        await _say(session, chat, 2, COMPANY, 5 + i, VACATION, label="ack", substantive=False, actor=BOT)
    result = await rebuild_interactions(session, now=T0 + timedelta(hours=2), persist=False)
    items = [item for item in result["items"] if item.chat_id in {chat.id for chat in chats}]
    assert len(items) == 3
    assert all(item.version == 3 and item.first_reaction_at is None for item in items)


@requires_db
@pytest.mark.parametrize("version", [2, 3])
async def test_request_after_answer_to_specialist_question_gets_own_timer(session, monkeypatch, version):
    """Законченный второй слой не поглощает просьбу после ответа клиента.

    В v2 фиксируем прежний результат для неизменности истории; в v3
    проверяем срок новой просьбы, в том числе после записи в базу.
    """
    from sqlalchemy import select
    from app.db.models import Interaction

    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False)
    monkeypatch.setattr(get_settings(), "episode_rules_v2_since", T0)
    monkeypatch.setattr(get_settings(), "episode_rules_v3_since", T0 if version == 3 else None)
    chat = await _chat(session, -100778620)
    manager = await _staff(session, "Менеджер")
    specialist = await _staff(session, "Специалист")
    old = await _say(session, chat, 1, CLIENT, 0, "Нужен договор", label="request", requires=True)
    await _say(session, chat, 2, COMPANY, 1, "Передала юристу", label="handoff", substantive=False, staff=manager)
    question = await _say(session, chat, 3, COMPANY, 5, "Для какой компании?", label="question", substantive=False, staff=specialist)
    answer = await _say(session, chat, 4, CLIENT, 6, "Для нашего ООО", label="request", requires=True)
    new = await _say(session, chat, 5, CLIENT, 7, "Ещё подготовьте акт", label="request", requires=True)
    result = await rebuild_interactions(session, now=T0 + timedelta(hours=2), persist=False)
    items = {item.opened_by_message_id: item for item in result["items"] if item.chat_id == chat.id}
    assert answer.id in result["answers"] and new.id not in result["answers"]
    assert items[old.id].substantive_message_id == question.id
    if version == 3:
        assert set(items) == {old.id, new.id}
        assert items[new.id].opened_at == new.sent_at
        assert items[new.id].first_reaction_at is None
        assert items[old.id].state is InteractionState.ANSWERED
    else:
        assert set(items) == {old.id}
    await rebuild_interactions(session, now=T0 + timedelta(hours=2), persist=True)
    await session.flush()
    stored = (await session.scalars(select(Interaction).where(Interaction.chat_id == chat.id))).all()
    assert {item.opened_by_message_id for item in stored} == set(items)
