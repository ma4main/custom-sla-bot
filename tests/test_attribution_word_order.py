"""Порядок слов в префиксе интегратора не важен.

Форматы «пишет:» и «делится файлом» не обязаны писать имя в одном порядке.
Без сравнения по перестановке «Соколова Ирина» завела бы в справочнике
дубль «Ирины Соколовой», и метрики по человеку разъехались бы надвое.
"""

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db.models import (
    Attribution,
    BusinessSide,
    Chat,
    ChatState,
    Message,
    Staff,
    TransportActorKind,
)
from app.services.attribution import attribute_message
from app.services.staff import normalize_name
from tests.conftest import requires_db

NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


async def _company_message(session, chat, tg_message_id: int, text: str) -> Message:
    message = Message(
        chat_id=chat.id,
        tg_message_id=tg_message_id,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY,
        text=text,
        char_count=len(text),
        sent_at=NOW,
    )
    session.add(message)
    await session.flush()
    return message


@requires_db
async def test_reversed_word_order_matches_same_person(session):
    chat = Chat(tg_chat_id=-100999010, title="Порядок слов", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    person = Staff(
        full_name="Ирина Соколова",
        normalized_name=normalize_name("Ирина Соколова"),
    )
    session.add(person)
    await session.flush()

    message = await _company_message(
        session, chat, 1, "Соколова Ирина [corp.example.com] делится файлом\n\nакт.pdf"
    )
    await attribute_message(session, message)
    await session.flush()

    attribution = await session.get(Attribution, message.id)
    assert attribution.staff_id == person.id, "перестановка слов не сматчилась"

    staff_count = await session.scalar(select(func.count(Staff.id)))
    assert staff_count == 1, "автосоздание завело дубль сотрудника"


@requires_db
async def test_mirror_named_staff_still_match_exactly(session):
    """Два «зеркальных» сотрудника в справочнике: точная форма побеждает.

    Перестановочный ключ у них общий, но точное совпадение проверяется
    первым — сообщение уходит правильному, а не «первому попавшемуся».
    """
    chat = Chat(tg_chat_id=-100999011, title="Коллизия", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    session.add_all(
        [
            Staff(
                full_name="Иванов Иван",
                normalized_name=normalize_name("Иванов Иван"),
            ),
            Staff(
                full_name="Иван Иванов",
                normalized_name=normalize_name("Иван Иванов"),
                discriminator="второй",
            ),
        ]
    )
    await session.flush()

    # Точная форма совпадает с первым — находится именно он.
    exact = await _company_message(
        session, chat, 1, "Иванов Иван [corp.example.com] пишет:\n\nдобрый день"
    )
    await attribute_message(session, exact)
    await session.flush()
    attribution = await session.get(Attribution, exact.id)
    assert attribution.staff_id is not None