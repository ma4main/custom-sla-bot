"""Экран «Снятые алерты» и учёт снятых живых обращений: снятое живое обращение
считается «ответа не требовали» во всех отчётах, и сумма показанных чисел
сходится с общим.
"""

from datetime import datetime, timedelta, timezone

from app.db.models import (
    BotRole,
    BotUser,
    BotUserState,
    BreachDismissal,
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.dismissals import dismissed_page
from app.services.report_data import load_speed
from app.services.report_lab import problem_chats
from app.services.tracking import open_period
from tests.conftest import requires_db

START = datetime(2026, 9, 1, 0, tzinfo=timezone.utc)
END = START + timedelta(days=2)


async def _owner(session) -> BotUser:
    owner = BotUser(
        tg_user_id=880800,
        display_name="Дмитрий",
        role=BotRole.OWNER,
        permissions={},
        state=BotUserState.ACTIVE,
    )
    session.add(owner)
    await session.flush()
    return owner


async def _case(session, chat: Chat, index: int, state: InteractionState) -> Message:
    opened_at = START + timedelta(hours=index)
    message = Message(
        chat_id=chat.id,
        tg_message_id=index,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text=f"Пришлите акт сверки {index}",
        char_count=20,
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
    return message


async def _chat(session, tg_chat_id: int = -100777001) -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title="ВЕКТОР", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=START - timedelta(days=1))
    return chat


@requires_db
async def test_dismissed_live_case_leaves_waiting_line(session):
    """Снятый живой кейс уходит из «ждут ответа» — как из среза и внимания."""
    chat = await _chat(session)
    owner = await _owner(session)
    open_msg = await _case(session, chat, 1, InteractionState.OPEN)
    await _case(session, chat, 2, InteractionState.REACTED)

    before = await load_speed(session, START, END)
    assert before["waiting"] == 2

    session.add(
        BreachDismissal(
            chat_id=chat.id,
            opened_by_message_id=open_msg.id,
            dismissed_by=owner.id,
        )
    )
    await session.flush()

    after = await load_speed(session, START, END)
    assert after["waiting"] == 1, (
        "снятое живое обращение обязано уйти из строки «ждут ответа» — "
        "кнопка под ней его уже не показывает"
    )
    assert after["no_response"] == 1, (
        "…и попасть в «ответа не требовали»: руководитель решил ровно это"
    )


@requires_db
async def test_sum_still_matches_total_after_dismissal(session):
    """Снятие не должно ронять обращение мимо всех показанных состояний."""
    chat = await _chat(session)
    owner = await _owner(session)
    open_msg = await _case(session, chat, 1, InteractionState.OPEN)
    await _case(session, chat, 2, InteractionState.ANSWERED)
    await _case(session, chat, 3, InteractionState.ABANDONED)
    session.add(
        BreachDismissal(
            chat_id=chat.id,
            opened_by_message_id=open_msg.id,
            dismissed_by=owner.id,
        )
    )
    await session.flush()

    speed = await load_speed(session, START, END)
    shown = (
        speed["answered"] + speed["no_response"] + speed["waiting"] + speed["timed_out"]
    )
    assert shown == speed["total"] == 3, (
        f"после снятия сумма показанных перестала сходиться с общим: {speed}"
    )


@requires_db
async def test_problem_chats_ignore_dismissed_waiting(session):
    """Колонка «Ждут» в HTML живёт по тому же правилу, что и остальные."""
    chat = await _chat(session)
    owner = await _owner(session)
    open_msg = await _case(session, chat, 1, InteractionState.OPEN)

    assert [row["waiting"] for row in await problem_chats(session, START, END)] == [1]

    session.add(
        BreachDismissal(
            chat_id=chat.id,
            opened_by_message_id=open_msg.id,
            dismissed_by=owner.id,
        )
    )
    await session.flush()

    assert await problem_chats(session, START, END) == [], (
        "чат без реальных просрочек и без реального ожидания в список "
        "проблемных попадать не должен"
    )


@requires_db
async def test_journal_shows_who_and_when(session):
    """Строка журнала несёт чат, обращение, кто и когда."""
    chat = await _chat(session)
    owner = await _owner(session)
    message = await _case(session, chat, 1, InteractionState.OPEN)
    session.add(
        BreachDismissal(
            chat_id=chat.id,
            opened_by_message_id=message.id,
            dismissed_by=owner.id,
            dismissed_at=START + timedelta(days=1),
        )
    )
    await session.flush()

    items, total = await dismissed_page(session, START, END)

    assert total == 1
    row = items[0]
    assert row["title"] == "ВЕКТОР"
    assert row["who"] == "Дмитрий"
    assert row["chat_id"] == chat.id
    assert row["opened_by_message_id"] == message.id
    assert row["opener_text"].startswith("Пришлите акт")
    assert row["state"] is InteractionState.OPEN, "видно, чем кейс живёт сейчас"


@requires_db
async def test_journal_period_is_by_decision_not_by_case(session):
    """Период журнала — по дате РЕШЕНИЯ: снять могут кейс любой давности."""
    chat = await _chat(session)
    owner = await _owner(session)
    message = await _case(session, chat, 1, InteractionState.OPEN)
    # Обращение внутри START..END, а решение — позже периода.
    session.add(
        BreachDismissal(
            chat_id=chat.id,
            opened_by_message_id=message.id,
            dismissed_by=owner.id,
            dismissed_at=END + timedelta(days=3),
        )
    )
    await session.flush()

    _, inside = await dismissed_page(session, START, END)
    assert inside == 0, "решение вне периода — строки быть не должно"

    _, later = await dismissed_page(session, END, END + timedelta(days=5))
    assert later == 1, "старое обращение, снятое позже, обязано попасть в журнал"


@requires_db
async def test_journal_keeps_decision_without_interaction(session):
    """Обращение могло не пережить пересборку — решение из журнала не пропадает."""
    chat = await _chat(session)
    owner = await _owner(session)
    message = Message(
        chat_id=chat.id,
        tg_message_id=99,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Где документы?",
        char_count=14,
        sent_at=START + timedelta(hours=1),
    )
    session.add(message)
    await session.flush()
    session.add(
        BreachDismissal(
            chat_id=chat.id,
            opened_by_message_id=message.id,
            dismissed_by=owner.id,
            dismissed_at=START + timedelta(hours=2),
        )
    )
    await session.flush()

    items, total = await dismissed_page(session, START, END)

    assert total == 1
    assert items[0]["state"] is None
    assert items[0]["opener_text"] == "Где документы?"
