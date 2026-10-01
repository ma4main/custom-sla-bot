"""Не больше одного ОТКРЫТОГО алерта каждого слоя на чат.

Пока по чату горит неотработанный алерт «нет реакции», вторая просрочка
первого слоя в том же чате владельца вторым сообщением не будит. Но и
потеряться она не должна: запись в журнале появляется — покрытой
(`covered_by_id`), — и дальше живёт на общих правах:

  - сводка и метрики читают alert_log и видят просрочку как обычно;
  - при закрытии покрывающего алерта покрытые обращения тоже считаются
    оповещёнными — своего сообщения у них не было и не будет.

Слои независимы: по чату могут гореть два алерта разных видов. Покрывающий
алерт закрывается только когда закрыты ВСЕ покрытые им обращения — второй
слой закрывают адресно, по одной теме, и остальные передачи остаются висеть.

Схлопывание пачек (30 минут) правило не подменяет: разрыв в тестах взят
35 минут — это ДВА самостоятельных события для движка.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
from app.services.alerts import (
    KIND_NO_REACTION,
    KIND_NO_SUBSTANTIVE,
    process_alerts,
    retry_undelivered,
    strike_closed_alerts,
)
from app.services.settings_store import MODE_ON, set_value
from app.services.tracking import open_period
from tests.conftest import requires_db

# Понедельник, 13:00 МСК — рабочее время графика по умолчанию.
NOW = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
OWNER = 930001


class FakeBot:
    """Принимает всё и возвращает номера сообщений — как настоящий."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edits: list[dict] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=5000 + len(self.sent))

    async def edit_message_text(self, **kwargs) -> None:
        self.edits.append(kwargs)


class DeadBot(FakeBot):
    """Телеграм недоступен: алерт записан, но никому не доставлен."""

    async def send_message(self, chat_id: int, text: str, **kwargs):
        raise RuntimeError("Bad Gateway")


async def _prepare(session, tg_chat_id: int, *, substantive: bool = False) -> Chat:
    session.add(
        BotUser(
            tg_user_id=OWNER,
            role=BotRole.OWNER,
            permissions={},
            state=BotUserState.ACTIVE,
        )
    )
    chat = Chat(tg_chat_id=tg_chat_id, title="Сигма", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=7))
    await set_value(session, "alerts", "enabled", True, actor_id=None)
    await set_value(session, "alerts", "respect_quiet_hours", False, actor_id=None)
    await set_value(session, "alerts", "threshold_minutes", 30, actor_id=None)
    if substantive:
        await set_value(session, "alerts", "substantive_mode", MODE_ON, actor_id=None)
    await session.flush()
    return chat


async def _client_message(session, chat: Chat, tg_message_id: int, at: datetime):
    message = Message(
        chat_id=chat.id,
        tg_message_id=tg_message_id,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text=f"Вопрос {tg_message_id}",
        char_count=12,
        sent_at=at,
    )
    session.add(message)
    await session.flush()
    return message


async def _waiting(session, chat: Chat, message: Message, **fields) -> Interaction:
    interaction = Interaction(
        chat_id=chat.id,
        opened_at=message.sent_at,
        opened_by_message_id=message.id,
        last_client_at=message.sent_at,
        client_messages=1,
        state=fields.pop("state", InteractionState.OPEN),
        **fields,
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def _handed_off(session, chat: Chat, message: Message, **fields) -> Interaction:
    """Обращение второго слоя: реакция была, вопрос передан специалисту."""
    return await _waiting(
        session,
        chat,
        message,
        state=InteractionState.REACTED,
        first_reaction_at=message.sent_at + timedelta(minutes=2),
        handoff_at=message.sent_at + timedelta(minutes=3),
        **fields,
    )


async def _journal(session, kind: str | None = None) -> list[AlertLog]:
    query = select(AlertLog).order_by(AlertLog.id)
    if kind is not None:
        query = query.where(AlertLog.kind == kind)
    return list((await session.scalars(query)).all())


@requires_db
async def test_second_overdue_of_the_same_layer_is_covered_not_sent(session):
    """Два просрочённых обращения первого слоя с разрывом 35 минут."""
    chat = await _prepare(session, -100930001)
    first = await _client_message(session, chat, 101, NOW - timedelta(hours=4))
    second = await _client_message(
        session, chat, 102, NOW - timedelta(hours=4) + timedelta(minutes=35)
    )
    await _waiting(session, chat, first)
    await _waiting(session, chat, second)

    bot = FakeBot()
    result = await process_alerts(session, bot)
    await session.flush()

    assert len(bot.sent) == 1, (
        "по чату ушло больше одного сообщения о молчании первого слоя: "
        f"{[text.splitlines()[0] for _, text in bot.sent]}"
    )
    assert result["sent"] == 1 and result["covered"] == 1

    journal = await _journal(session)
    assert len(journal) == 2, "вторая просрочка не попала в журнал — сводка её не увидит"
    anchor, covered = journal
    assert anchor.opened_by_message_id == first.id
    assert anchor.covered_by_id is None and anchor.recipients == [OWNER]
    assert covered.opened_by_message_id == second.id
    assert covered.covered_by_id == anchor.id, "вторая просрочка не помечена покрытой"
    assert covered.recipients == [] and covered.delivered is True
    assert covered.shadow is False, "покрытая запись не теневая: сводка обязана её видеть"


@requires_db
async def test_reaction_closes_the_alert_for_both_requests(session):
    """Реакция менеджера: алерт закрыт, оба обращения считаются оповещёнными."""
    chat = await _prepare(session, -100930002)
    first = await _client_message(session, chat, 201, NOW - timedelta(hours=4))
    second = await _client_message(
        session, chat, 202, NOW - timedelta(hours=4) + timedelta(minutes=35)
    )
    one = await _waiting(session, chat, first)
    two = await _waiting(session, chat, second)

    bot = FakeBot()
    await process_alerts(session, bot)
    await session.flush()

    # Менеджер ответил — движок снял ожидания по обоим обращениям.
    reacted_at = NOW - timedelta(hours=1)
    for interaction in (one, two):
        interaction.state = InteractionState.ANSWERED
        interaction.first_reaction_at = reacted_at
        interaction.ttfr_business_seconds = 3600
    await session.flush()

    struck = await strike_closed_alerts(session, bot)
    await session.flush()

    assert struck == 1
    anchor, covered = await _journal(session)
    assert anchor.struck_at is not None, "отработанный алерт остался открытым"
    assert bot.edits and "Нет реакции — отработано" in bot.edits[0]["text"]
    # У покрытой записи своего сообщения нет — править нечего, и правило
    # «оповещены обе» держится именно записью в журнале.
    assert covered.covered_by_id == anchor.id and covered.message_ids == {}


@requires_db
async def test_after_closing_the_next_overdue_wakes_the_owner_again(session):
    """Закрыт — значит закрыт: третья просрочка даёт новое сообщение."""
    chat = await _prepare(session, -100930003)
    first = await _client_message(session, chat, 301, NOW - timedelta(hours=6))
    second = await _client_message(
        session, chat, 302, NOW - timedelta(hours=6) + timedelta(minutes=35)
    )
    one = await _waiting(session, chat, first)
    two = await _waiting(session, chat, second)

    bot = FakeBot()
    await process_alerts(session, bot)
    await session.flush()
    assert len(bot.sent) == 1

    for interaction in (one, two):
        interaction.state = InteractionState.ANSWERED
        interaction.first_reaction_at = NOW - timedelta(hours=3)
        interaction.ttfr_business_seconds = 3600
    await session.flush()
    await strike_closed_alerts(session, bot)
    await session.flush()

    third = await _client_message(session, chat, 303, NOW - timedelta(hours=2))
    await _waiting(session, chat, third)

    result = await process_alerts(session, bot)
    await session.flush()

    assert result["sent"] == 1 and result["covered"] == 0
    assert len(bot.sent) == 2, "после закрытия алерта новая просрочка снова молчит"
    journal = await _journal(session)
    assert [entry.covered_by_id for entry in journal] == [None, journal[0].id, None]


@requires_db
async def test_layers_are_independent(session):
    """По чату могут гореть два алерта — первого и второго слоя."""
    chat = await _prepare(session, -100930004, substantive=True)
    silent = await _client_message(session, chat, 401, NOW - timedelta(hours=4))
    handed = await _client_message(session, chat, 402, NOW - timedelta(days=3))
    await _waiting(session, chat, silent)
    await _handed_off(session, chat, handed)

    bot = FakeBot()
    result = await process_alerts(session, bot)
    await session.flush()

    assert result["sent"] == 2 and result["covered"] == 0, (
        "правило одного алерта на чат склеило разные слои: «нет реакции» и "
        "«нет ответа специалиста» — разные события и разные адресаты решений"
    )
    kinds = {entry.kind for entry in await _journal(session)}
    assert kinds == {KIND_NO_REACTION, KIND_NO_SUBSTANTIVE}


@requires_db
async def test_second_layer_stays_open_while_one_topic_still_waits(session):
    """Второй слой: адресно закрыта одна тема — алерт остаётся открытым."""
    chat = await _prepare(session, -100930005, substantive=True)
    first = await _client_message(session, chat, 501, NOW - timedelta(days=3))
    second = await _client_message(
        session, chat, 502, NOW - timedelta(days=3) + timedelta(hours=2)
    )
    one = await _handed_off(session, chat, first)
    await _handed_off(session, chat, second)

    bot = FakeBot()
    result = await process_alerts(session, bot)
    await session.flush()
    assert (result["sent"], result["covered"]) == (1, 1)

    # Специалист ответил адресно только по первой теме.
    one.state = InteractionState.ANSWERED
    one.substantive_at = NOW - timedelta(hours=2)
    one.ttfa_business_seconds = 7200
    await session.flush()

    struck = await strike_closed_alerts(session, bot)
    await session.flush()

    assert struck == 0, "алерт зачёркнут, хотя вторая передача всё ещё ждёт ответа"
    anchor, covered = await _journal(session)
    assert anchor.struck_at is None
    assert covered.covered_by_id == anchor.id

    # И новое сообщение по этому слою не уходит: алерт всё ещё горит.
    third = await _client_message(session, chat, 503, NOW - timedelta(days=2))
    await _handed_off(session, chat, third)
    again = await process_alerts(session, bot)
    await session.flush()
    assert (again["sent"], again["covered"]) == (0, 1)
    assert len(bot.sent) == 1


@requires_db
async def test_retry_does_not_duplicate_covered_requests(session):
    """Досылка не превращает покрытую запись во второе сообщение."""
    chat = await _prepare(session, -100930006)
    first = await _client_message(session, chat, 601, NOW - timedelta(hours=4))
    second = await _client_message(
        session, chat, 602, NOW - timedelta(hours=4) + timedelta(minutes=35)
    )
    await _waiting(session, chat, first)
    await _waiting(session, chat, second)

    bot = FakeBot()
    await process_alerts(session, bot)
    await session.flush()
    assert len(bot.sent) == 1

    # Даже если запись окажется помеченной недоставленной (сбой, ручная
    # правка, будущая доработка), досылать по ней нечего: сообщения у неё
    # нет и не было.
    _, covered = await _journal(session)
    covered.delivered = False
    await session.flush()

    recovered = await retry_undelivered(session, bot)
    await session.flush()

    assert recovered == 0
    assert len(bot.sent) == 1, "досылка отправила второе сообщение по покрытой просрочке"


@requires_db
async def test_undelivered_alert_does_not_cover_the_next_request(session):
    """Недоставленный алерт никого не оповестил — молчать за него нельзя."""
    chat = await _prepare(session, -100930007)
    first = await _client_message(session, chat, 701, NOW - timedelta(hours=4))
    await _waiting(session, chat, first)

    dead = DeadBot()
    await process_alerts(session, dead)
    await session.flush()
    anchor = (await _journal(session))[0]
    assert anchor.recipients == [] and anchor.delivered is False

    second = await _client_message(
        session, chat, 702, NOW - timedelta(hours=4) + timedelta(minutes=35)
    )
    await _waiting(session, chat, second)

    bot = FakeBot()
    result = await process_alerts(session, bot)
    await session.flush()

    assert result["sent"] == 1 and result["covered"] == 0, (
        "вторая просрочка промолчала в расчёте на алерт, которого владелец "
        "никогда не видел"
    )
    assert [entry.covered_by_id for entry in await _journal(session)] == [None, None]
