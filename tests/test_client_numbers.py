"""Номера людей со стороны клиента: «Клиент 1», «Клиент 2».

Со стороны клиента часто пишут двое. Без номеров алерт показывал вопрос одного
(«Могу на почту направить?») и «Давай» другого как «Клиент» и «Последнее от
клиента» — будто клиент отвечает сам себе. Проверяется:
  - номер — по первому сообщению в чате, один человек — без номеров;
  - алерт, выписка и «Участники» подписывают человека одним номером.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.alerts import KIND_NO_REACTION, build_alert_text
from app.services.sender_rules import window_participants
from app.services.transcript import client_numbers, render_transcript
from tests.conftest import requires_db

BASE = datetime(2026, 9, 23, 13, 59, tzinfo=timezone.utc)
MSK = ZoneInfo("Europe/Moscow")
NOW = BASE + timedelta(hours=18)
CALENDAR = {"timezone": "Europe/Moscow", "start": "10:00", "end": "17:00"}

ASKER = 100200311
COLLEAGUE = 100200312


def _message(
    tg_message_id: int,
    tg_user_id: int | None,
    text: str,
    minutes: int,
    side: BusinessSide = BusinessSide.CLIENT,
    chat_id: int = 1,
) -> Message:
    return Message(
        id=tg_message_id,
        chat_id=chat_id,
        tg_message_id=tg_message_id,
        tg_user_id=tg_user_id,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=side,
        side_rule_version=5,
        text=text,
        char_count=len(text),
        has_media=False,
        needs_reclassification=False,
        sent_at=BASE + timedelta(minutes=minutes),
    )


def _alert(numbers: dict[int, int] | None) -> str:
    opener = _message(1, ASKER, "Могу на почту направить ?", 0)
    last = _message(2, COLLEAGUE, "Давай", 16)
    interaction = Interaction(
        chat_id=1,
        opened_at=opener.sent_at,
        opened_by_message_id=opener.id,
        last_client_at=last.sent_at,
        client_messages=2,
        state=InteractionState.OPEN,
        version=4,
    )
    return build_alert_text(
        kind=KIND_NO_REACTION,
        chat_title="Бухгалтерия",
        opener=opener,
        interaction=interaction,
        last_client=last,
        reaction=None,
        last_company=None,
        deadline=BASE + timedelta(hours=17, minutes=30),
        calendar_age=int((NOW - BASE).total_seconds()),
        limit_minutes=30,
        calendar_cfg=CALENDAR,
        now=NOW,
        client_numbers=numbers,
    )


def test_alert_tells_two_client_people_apart():
    text = _alert({COLLEAGUE: 1, ASKER: 2})
    assert "👤 <b>Клиент 2</b> — вчера в 16:59" in text
    assert "👤 <b>Последнее от клиента 1</b> — вчера в 17:15" in text


def test_alert_without_numbers_keeps_plain_client():
    text = _alert({})
    assert "👤 <b>Клиент</b> — вчера в 16:59" in text
    assert "👤 <b>Последнее от клиента</b> — вчера в 17:15" in text


def test_transcript_uses_the_same_numbers():
    rows = [
        (_message(1, ASKER, "Могу на почту направить ?", 0), None),
        (_message(2, COLLEAGUE, "Давай", 16), None),
    ]
    body = render_transcript(rows, MSK, NOW, client_numbers={COLLEAGUE: 1, ASKER: 2})
    assert "🔵 клиент 2: Могу на почту направить ?" in body
    assert "🔵 клиент 1: Давай" in body

    plain = render_transcript(rows, MSK, NOW)
    assert "🔵 клиент: Давай" in plain


async def _chat(session, tg_chat_id: int) -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title="Бухгалтерия", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    return chat


def _stored(chat: Chat, tg_message_id: int, tg_user_id: int | None, minutes: int, side=None):
    message = _message(
        tg_message_id, tg_user_id, f"сообщение {tg_message_id}", minutes,
        side or BusinessSide.CLIENT, chat.id,
    )
    message.id = None  # id выдаёт база: порядок id = порядок сообщений
    return message


@requires_db
async def test_numbers_follow_first_message_in_chat(session):
    chat = await _chat(session, -1009240001)
    session.add_all(
        [
            _stored(chat, 1, COLLEAGUE, 0),
            _stored(chat, 2, 100200313, 1, BusinessSide.COMPANY),
            _stored(chat, 3, ASKER, 2),
            _stored(chat, 4, COLLEAGUE, 3),
            _stored(chat, 5, None, 4),
        ]
    )
    await session.flush()

    assert await client_numbers(session, chat.id) == {COLLEAGUE: 1, ASKER: 2}


@requires_db
async def test_single_client_person_gets_no_number(session):
    chat = await _chat(session, -1009240002)
    session.add_all(
        [
            _stored(chat, 1, ASKER, 0),
            _stored(chat, 2, 100200313, 1, BusinessSide.COMPANY),
            _stored(chat, 3, ASKER, 2),
        ]
    )
    await session.flush()

    assert await client_numbers(session, chat.id) == {}


@requires_db
async def test_participants_show_the_client_number(session):
    chat = await _chat(session, -1009240003)
    first = _stored(chat, 1, COLLEAGUE, 0)
    second = _stored(chat, 2, ASKER, 1)
    session.add_all([first, second])
    await session.flush()

    rows = await window_participants(session, chat.id, first.id, second.id)
    labels = {row["key"]: row["label"] for row in rows}
    assert labels[COLLEAGUE].endswith("(клиент 1)")
    assert labels[ASKER].endswith("(клиент 2)")
