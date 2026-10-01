"""Массовая атрибуция на объёме больше одной партии (1001 строка): обработанные
строки выпадают из выборки, и растущий offset пропускал бы следующую партию."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db.models import (
    Attribution,
    AttributionMethod,
    BusinessSide,
    Chat,
    ChatState,
    Message,
    Staff,
    TransportActorKind,
)
from app.services.attribution import attribute_all
from app.services.staff import normalize_name
from tests.conftest import requires_db

TOTAL = 1001


@requires_db
async def test_attribute_all_processes_every_message(session):
    chat = Chat(tg_chat_id=-100999001, title="Нагрузочный", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()

    session.add(
        Staff(
            full_name="Ирина Соколова",
            normalized_name=normalize_name("Ирина Соколова"),
        )
    )

    base = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
    for index in range(TOTAL):
        session.add(
            Message(
                chat_id=chat.id,
                tg_message_id=index + 1,
                transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
                business_side=BusinessSide.COMPANY,
                text=f"Ирина Соколова [corp.example.com] пишет:\n\nсообщение {index}",
                char_count=40,
                sent_at=base + timedelta(seconds=index),
            )
        )
    await session.flush()

    result = await attribute_all(session, batch_size=500)

    assert result["processed"] == TOTAL, "часть партий пропущена"
    total_rows = await session.scalar(
        select(func.count(Attribution.message_id))
        .join(Message, Message.id == Attribution.message_id)
        .where(Message.chat_id == chat.id)
    )
    assert total_rows == TOTAL
    resolved = await session.scalar(
        select(func.count(Attribution.message_id))
        .join(Message, Message.id == Attribution.message_id)
        .where(Message.chat_id == chat.id)
        .where(Attribution.staff_id.isnot(None))
    )
    assert resolved == TOTAL, "автор определён не у всех"


@requires_db
async def test_manual_attribution_is_never_overwritten(session):
    """Ручное решение человека сильнее любого будущего парсера."""
    chat = Chat(tg_chat_id=-100999002, title="Ручная разметка", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()

    person = Staff(
        full_name="Пётр Смирнов",
        normalized_name=normalize_name("Пётр Смирнов"),
    )
    session.add(person)
    await session.flush()

    message = Message(
        chat_id=chat.id,
        tg_message_id=1,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY,
        text="Без узнаваемого префикса",
        char_count=24,
        sent_at=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
    )
    session.add(message)
    await session.flush()

    session.add(
        Attribution(
            message_id=message.id,
            staff_id=person.id,
            method=AttributionMethod.MANUAL,
            confidence=1.0,
            parser_version=0,
            raw_name=None,
        )
    )
    await session.flush()

    await attribute_all(session, batch_size=500)

    kept = await session.scalar(
        select(Attribution).where(Attribution.message_id == message.id)
    )
    assert kept.method is AttributionMethod.MANUAL
    assert kept.staff_id == person.id
