"""Числа в отчёте обязаны сходиться с общим количеством.

Тест держит инвариант, а не конкретные числа: сколько бы состояний ни
появилось, показанные (включая снятые с ожидания по пределу времени)
обязаны складываться в total. Здесь же — тёзки чатов и сотрудников.
"""

from datetime import datetime, timedelta, timezone

from app.db.models import (
    Attribution,
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    Staff,
    TransportActorKind,
)
from app.services.report_data import (
    load_chat_report,
    load_speed,
    load_staff_report,
    load_summary,
)
from app.services.tracking import close_period, open_period
from tests.conftest import requires_db

START = datetime(2026, 8, 24, 0, tzinfo=timezone.utc)
END = START + timedelta(days=1)

# По одному обращению в каждом состоянии, включая снятое по пределу времени.
STATES = [
    InteractionState.ANSWERED,
    InteractionState.NO_RESPONSE_NEEDED,
    InteractionState.OPEN,
    InteractionState.REACTED,
    InteractionState.ABANDONED,
]


async def _chat_with_every_state(session) -> Chat:
    chat = Chat(tg_chat_id=-100555001, title="Арифметика", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=START - timedelta(days=1))

    for index, state in enumerate(STATES, start=1):
        opened_at = START + timedelta(hours=index)
        message = Message(
            chat_id=chat.id,
            tg_message_id=index,
            transport_actor_kind=TransportActorKind.HUMAN_USER,
            business_side=BusinessSide.CLIENT,
            text=f"Обращение {index}",
            char_count=12,
            sent_at=opened_at,
        )
        session.add(message)
        await session.flush()
        session.add(
            Interaction(
                chat_id=chat.id,
                opened_at=opened_at,
                opened_by_message_id=message.id,
                last_client_at=opened_at,
                client_messages=1,
                state=state,
            )
        )
    await session.flush()
    return chat


@requires_db
async def test_shown_counters_sum_to_total(session):
    await _chat_with_every_state(session)

    speed = await load_speed(session, START, END)

    shown = (
        speed["answered"]
        + speed["no_response"]
        + speed["waiting"]
        + speed["timed_out"]
    )
    assert shown == speed["total"] == len(STATES), (
        "показанные числа не складываются в общее — значит какое-то состояние "
        f"снова не выводится: {speed}"
    )


@requires_db
async def test_timed_out_is_counted_separately(session):
    """Снятое по пределу не должно прятаться ни в «ждут», ни в «отвечено»."""
    await _chat_with_every_state(session)

    speed = await load_speed(session, START, END)

    assert speed["timed_out"] == 1
    assert speed["waiting"] == 2, "ждут — это только открытые и отреагировавшие"
    assert speed["answered"] == 1


@requires_db
async def test_duration_limit_is_reported_for_the_explanation(session):
    """Пояснение к числу берёт предел из настроек, а не зашитую цифру."""
    await _chat_with_every_state(session)

    speed = await load_speed(session, START, END)

    assert speed["wait_reaction_hours"] == 24


# ═══════════════════════════════════════════════════════════════
# Тёзки не склеиваются
# ═══════════════════════════════════════════════════════════════


@requires_db
async def test_same_chat_title_stays_two_rows(session):
    """Два чата «Бухгалтерия» — две строки в отчёте сотрудника, не одна.

    Одинаковые названия групп в Telegram обычны: группировка по названию
    молча сложила бы нагрузку разных клиентов в одну строку.
    """
    person = Staff(full_name="Тёзкин Тест", normalized_name="тёзкин тест")
    session.add(person)
    await session.flush()

    chats = []
    for tg_id in (-100777301, -100777302):
        chat = Chat(tg_chat_id=tg_id, title="Бухгалтерия", state=ChatState.TRACKED)
        session.add(chat)
        await session.flush()
        await open_period(session, chat, reason="test", at=START)
        chats.append(chat)

    for index, chat in enumerate(chats):
        message = Message(
            chat_id=chat.id,
            tg_message_id=index + 1,
            transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
            business_side=BusinessSide.COMPANY,
            text="ответ",
            char_count=5,
            sent_at=START + timedelta(hours=1 + index),
        )
        session.add(message)
        await session.flush()
        session.add(
            Attribution(
                message_id=message.id, staff_id=person.id, parser_version=2
            )
        )
    await session.flush()

    data = await load_staff_report(session, person.id, START, END)

    assert len(data["chats"]) == 2, "чаты-тёзки склеились в одну строку"


@requires_db
async def test_same_staff_name_stays_two_rows(session):
    """Два «Иван Иванов» в отчёте чата — две строки, не одна."""
    chat = Chat(tg_chat_id=-100777303, title="Тёзки", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=START)

    people = []
    for discriminator in (None, "второй"):
        person = Staff(
            full_name="Иван Иванов",
            normalized_name="иван иванов",
            discriminator=discriminator,
        )
        session.add(person)
        await session.flush()
        people.append(person)

    for index, person in enumerate(people):
        message = Message(
            chat_id=chat.id,
            tg_message_id=100 + index,
            transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
            business_side=BusinessSide.COMPANY,
            text="ответ",
            char_count=5,
            sent_at=START + timedelta(hours=1 + index),
        )
        session.add(message)
        await session.flush()
        session.add(
            Attribution(
                message_id=message.id, staff_id=person.id, parser_version=2
            )
        )
    await session.flush()

    data = await load_chat_report(session, chat.id, START, END)

    assert len(data["staff"]) == 2, "сотрудники-тёзки склеились в одну строку"


@requires_db
async def test_archived_today_still_counts_for_yesterday(session):
    """«Чатов в анализе за период» — по наблюдению в периоде, не по сейчас.

    Чат, заархивированный сегодня, участвовал во вчерашней переписке
    и обязан считаться во вчерашнем отчёте.
    """
    chat = Chat(tg_chat_id=-100777304, title="Вчерашний", state=ChatState.ARCHIVED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=START)
    await close_period(session, chat, reason="test", at=END + timedelta(days=1))

    data = await load_summary(session, START, END)

    assert data["tracked"] >= 1, "архивный сегодня чат выпал из вчерашней выборки"
