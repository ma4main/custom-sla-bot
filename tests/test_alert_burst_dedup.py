"""Схлопывание очереди клиента (дубли соседних просьб) учитывает и закрытые обращения: закрытое
становится анкером, только если по нему уже горит алерт первого слоя; вовремя
закрытая просьба соседнюю не гасит, а поздний ответ не глушит всю очередь."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import (
    AlertLog,
    BotRole,
    BotUser,
    BotUserState,
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.alerts import KIND_NO_REACTION, process_alerts
from app.services.settings_store import set_value
from app.services.tracking import open_period
from tests.conftest import requires_db

# Понедельник, 13:00 МСК — рабочее время графика по умолчанию.
NOW = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
OWNER = 910001


class SilentBot:
    """Бот, который всё принимает: тест смотрит на журнал, а не на доставку."""

    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_message(self, chat_id: int, *args, **kwargs) -> None:
        self.sent.append(chat_id)


async def _prepare(session, tg_chat_id: int, threshold_minutes: int = 30) -> Chat:
    session.add(
        BotUser(
            tg_user_id=OWNER,
            role=BotRole.OWNER,
            permissions={},
            state=BotUserState.ACTIVE,
        )
    )
    chat = Chat(tg_chat_id=tg_chat_id, title="Очередь", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))
    await set_value(session, "alerts", "enabled", True, actor_id=None)
    await set_value(session, "alerts", "respect_quiet_hours", False, actor_id=None)
    await set_value(
        session, "alerts", "threshold_minutes", threshold_minutes, actor_id=None
    )
    await session.flush()
    return chat


async def _client_message(session, chat: Chat, tg_message_id: int, at: datetime, text: str):
    message = Message(
        chat_id=chat.id,
        tg_message_id=tg_message_id,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text=text,
        char_count=len(text),
        sent_at=at,
    )
    session.add(message)
    await session.flush()
    return message


async def _work(
    session,
    chat: Chat,
    message: Message,
    *,
    state: InteractionState,
    first_reaction_at: datetime | None = None,
) -> None:
    session.add(
        Interaction(
            chat_id=chat.id,
            opened_at=message.sent_at,
            opened_by_message_id=message.id,
            last_client_at=message.sent_at,
            client_messages=1,
            state=state,
            first_reaction_at=first_reaction_at,
        )
    )
    await session.flush()


async def _alerted_openers(session) -> set[int]:
    rows = await session.execute(select(AlertLog.opened_by_message_id))
    return set(rows.scalars())


async def _burning_alert(session, chat: Chat, message: Message) -> None:
    """Алерт первого слоя по обращению уже ушёл и ещё не закрыт.

    Срок анкера истекает раньше позднего ответа, и сообщение владельцу
    уходит ближайшим тиком — задолго до «принято».
    `sent_at` по умолчанию — сейчас, иначе запись выпадет за предел
    открытости в 48 часов.
    """
    session.add(
        AlertLog(
            chat_id=chat.id,
            opened_by_message_id=message.id,
            kind=KIND_NO_REACTION,
            recipients=[OWNER],
            delivered=True,
        )
    )
    await session.flush()


@requires_db
async def test_late_answered_anchor_still_collapses_the_duplicate(session):
    """По анкеру алерт уже горит — дубль обязан молчать."""
    chat = await _prepare(session, -100910001)
    opened_at = NOW - timedelta(hours=2)
    text = await _client_message(session, chat, 2716, opened_at, "Примите чек пожалуйста")
    attachment = await _client_message(
        session, chat, 2718, opened_at + timedelta(seconds=3), "[документ]"
    )
    # Общий ответ пришёл на 68 минут позже срока и сослался только на текст.
    await _work(
        session,
        chat,
        text,
        state=InteractionState.ANSWERED,
        first_reaction_at=opened_at + timedelta(minutes=98),
    )
    await _work(session, chat, attachment, state=InteractionState.OPEN)
    await _burning_alert(session, chat, text)

    await process_alerts(session, SilentBot())
    await session.flush()

    assert await _alerted_openers(session) == {text.id}, (
        "голое вложение через 3 секунды после текста той же просьбы подняло "
        "второй алерт об одном и том же молчании: анкер очереди выпал из "
        "схлопывания, потому что его уже погасили «веером»"
    )


@requires_db
async def test_whole_burst_answered_late_wakes_nobody(session):
    """Обе строки пачки закрыты одним поздним ответом.

    Будить некого — обе просрочки уходят в сводку строкой «закрыто
    с опозданием», а не сообщением.
    """
    chat = await _prepare(session, -100910005)
    opened_at = NOW - timedelta(hours=2)
    reaction_at = opened_at + timedelta(minutes=98)
    text = await _client_message(session, chat, 2716, opened_at, "Примите чек пожалуйста")
    attachment = await _client_message(
        session, chat, 2718, opened_at + timedelta(seconds=3), "[документ]"
    )
    for message in (text, attachment):
        await _work(
            session,
            chat,
            message,
            state=InteractionState.ANSWERED,
            first_reaction_at=reaction_at,
        )

    await process_alerts(session, SilentBot())
    await session.flush()

    assert await _alerted_openers(session) == set()


@requires_db
async def test_late_answered_anchor_does_not_silence_the_later_requests(session):
    """Анкер отвечен с опозданием — поздние просрочки обязаны звучать.

    Анкером становится самая ранняя непокрытая просрочка, следующая
    схлопывается к ней как дубль очереди.
    """
    chat = await _prepare(session, -100910006)
    opened_at = NOW - timedelta(hours=2)
    anchor = await _client_message(session, chat, 4040, opened_at, "В оплату")
    second = await _client_message(
        session, chat, 4042, opened_at + timedelta(seconds=29), "В оплату [счёт]"
    )
    third = await _client_message(
        session, chat, 4043, opened_at + timedelta(seconds=55), "В оплату [счёт]"
    )
    await _work(
        session,
        chat,
        anchor,
        state=InteractionState.ANSWERED,
        first_reaction_at=opened_at + timedelta(minutes=45),
    )
    await _work(session, chat, second, state=InteractionState.OPEN)
    await _work(session, chat, third, state=InteractionState.OPEN)

    await process_alerts(session, SilentBot())
    await session.flush()

    assert await _alerted_openers(session) == {second.id}, (
        "просрочки после позднего «принято» остались без единого алерта: "
        "анкером пачки стало обращение, которое само никого не будит"
    )


@requires_db
async def test_anchor_answered_in_time_leaves_the_later_request_its_alert(session):
    """Обратная сторона: вовремя закрытая просьба не гасит соседнюю."""
    chat = await _prepare(session, -100910002)
    opened_at = NOW - timedelta(hours=2)
    first = await _client_message(session, chat, 3001, opened_at, "Оплатите аренду")
    second = await _client_message(
        session, chat, 3002, opened_at + timedelta(seconds=30), "И зарплату подготовьте"
    )
    await _work(
        session,
        chat,
        first,
        state=InteractionState.ANSWERED,
        first_reaction_at=opened_at + timedelta(minutes=5),
    )
    await _work(session, chat, second, state=InteractionState.OPEN)

    await process_alerts(session, SilentBot())
    await session.flush()

    assert await _alerted_openers(session) == {second.id}, (
        "по ранней просьбе отреагировали в срок — она не анкер очереди "
        "молчания, и поздняя просьба обязана разбудить владельца"
    )


@requires_db
async def test_two_open_requests_of_one_burst_wake_the_owner_once(session):
    """Из двух открытых просьб одной пачки остаётся самая ранняя."""
    chat = await _prepare(session, -100910003)
    opened_at = NOW - timedelta(hours=2)
    first = await _client_message(session, chat, 4957, opened_at, "Это что за налог?")
    second = await _client_message(
        session, chat, 4958, opened_at + timedelta(minutes=1), "Объясните логику"
    )
    await _work(session, chat, first, state=InteractionState.OPEN)
    await _work(session, chat, second, state=InteractionState.OPEN)

    await process_alerts(session, SilentBot())
    await session.flush()

    assert await _alerted_openers(session) == {first.id}


@requires_db
async def test_requests_outside_the_burst_window_are_separate_events(session):
    """Разные просьбы за окном очереди — два события, а не дубли.

    Окно схлопывания — порог первой реакции: при пороге 5 минут просьбы
    с разницей в 10 минут очередью не считаются, даже если ранняя уже
    закрыта с опозданием.
    """
    chat = await _prepare(session, -100910004, threshold_minutes=5)
    opened_at = NOW - timedelta(hours=2)
    first = await _client_message(session, chat, 5001, opened_at, "Пришлите акт")
    second = await _client_message(
        session, chat, 5002, opened_at + timedelta(minutes=10), "Нужна справка о доходах"
    )
    await _work(
        session,
        chat,
        first,
        state=InteractionState.ANSWERED,
        first_reaction_at=opened_at + timedelta(minutes=40),
    )
    await _work(session, chat, second, state=InteractionState.OPEN)

    await process_alerts(session, SilentBot())
    await session.flush()

    assert await _alerted_openers(session) == {second.id}, (
        "просьба через 10 минут при окне 5 минут схлопнута как дубль"
    )
