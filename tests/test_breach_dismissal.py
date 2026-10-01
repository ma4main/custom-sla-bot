"""Решение «снять нарушение»: снятое обращение выпадает из срезов нарушений
и счётчиков, закрытое по пределу считается «ответа не требовалось», сумма
состояний сходится с total; решение обратимо и переживает пересборку.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import (
    AuditLog,
    BotRole,
    BotUser,
    BotUserState,
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    Staff,
    TransportActorKind,
)
from app.services.dismissals import dismiss, is_dismissed, restore
from app.services.report_data import load_speed
from app.services.report_drill import (
    KIND_BREACH_REACTION,
    KIND_NO_ANSWER,
    drill_counts,
)
from app.services.report_lab import problem_chats, staff_speed
from app.services.tracking import open_period
from tests.conftest import requires_db

NOW = datetime(2026, 8, 26, 10, tzinfo=timezone.utc)
START = NOW - timedelta(days=3)
END = NOW + timedelta(days=1)


async def _owner(session) -> BotUser:
    actor = BotUser(
        tg_user_id=880001, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
    )
    session.add(actor)
    await session.flush()
    return actor


async def _breach(session, chat: Chat, tg_message_id: int, **extra) -> Interaction:
    """Обращение с просрочкой реакции — базовый объект решений."""
    message = Message(
        chat_id=chat.id,
        tg_message_id=tg_message_id,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Вопрос",
        char_count=6,
        sent_at=START + timedelta(hours=tg_message_id),
    )
    session.add(message)
    await session.flush()
    interaction = Interaction(
        chat_id=chat.id,
        opened_at=message.sent_at,
        opened_by_message_id=message.id,
        last_client_at=message.sent_at,
        client_messages=1,
        state=extra.pop("state", InteractionState.ANSWERED),
        sla_breached=extra.pop("sla_breached", True),
        ttfr_business_seconds=extra.pop("ttfr_business_seconds", 3600),
        **extra,
    )
    session.add(interaction)
    await session.flush()
    return interaction


@requires_db
async def test_dismissal_removes_breach_from_slices_and_counters(session):
    owner = await _owner(session)
    chat = Chat(tg_chat_id=-100660001, title="Решение", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=START)

    kept = await _breach(session, chat, 1)
    dropped = await _breach(session, chat, 2)

    assert await dismiss(session, owner, chat.id, dropped.opened_by_message_id)
    await session.flush()

    counts = await drill_counts(session, START, END, chat.id)
    speed = await load_speed(session, START, END)

    assert counts[KIND_BREACH_REACTION] == 1, "снятое нарушение осталось в срезе"
    assert speed["breach_reaction"] == 1, "снятое нарушение осталось в счётчике"
    assert kept.opened_by_message_id != dropped.opened_by_message_id

    # След обязан остаться: решение меняет отчёты, по которым судят о людях.
    entry = await session.scalar(
        select(AuditLog).where(AuditLog.action == "breach.dismissed")
    )
    assert entry is not None and entry.payload["chat_id"] == chat.id


@requires_db
async def test_dismissed_abandoned_becomes_no_response_and_sum_holds(session):
    """Снятое «осталось без ответа» уходит в «ответа не требовалось»."""
    owner = await _owner(session)
    chat = Chat(tg_chat_id=-100660002, title="Файл в конце", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=START)

    await _breach(
        session, chat, 1, state=InteractionState.ANSWERED, sla_breached=False
    )
    abandoned = await _breach(
        session, chat, 2, state=InteractionState.ABANDONED, sla_breached=False
    )

    before = await load_speed(session, START, END)
    assert before["timed_out"] == 1

    await dismiss(session, owner, chat.id, abandoned.opened_by_message_id)
    await session.flush()

    speed = await load_speed(session, START, END)
    counts = await drill_counts(session, START, END, chat.id)

    assert speed["timed_out"] == 0, "снятое осталось «без ответа»"
    assert speed["no_response"] == 1, "снятое не перешло в «ответа не требовалось»"
    assert counts[KIND_NO_ANSWER] == 0
    # Инвариант отчёта: показанные состояния сходятся с общим числом.
    total_shown = (
        speed["answered"] + speed["no_response"] + speed["waiting"] + speed["timed_out"]
    )
    assert total_shown == speed["total"], "сумма состояний разошлась с total"


@requires_db
async def test_dismissal_is_reversible_and_survives_reset(session):
    owner = await _owner(session)
    chat = Chat(tg_chat_id=-100660003, title="Отмена", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=START)

    breach = await _breach(session, chat, 1)
    anchor = breach.opened_by_message_id

    await dismiss(session, owner, chat.id, anchor)
    # Повторное снятие — не ошибка и не дубль.
    assert not await dismiss(session, owner, chat.id, anchor)

    # Пересборка пересоздаёт Interaction — решение живёт по устойчивому
    # ключу (chat, открывающее сообщение) и обязано пережить это.
    await session.delete(breach)
    await session.flush()
    await _breach(session, chat, 5)  # другой якорь — решение его не касается
    recreated = Interaction(
        chat_id=chat.id,
        opened_at=START + timedelta(hours=1),
        opened_by_message_id=anchor,
        last_client_at=START + timedelta(hours=1),
        client_messages=1,
        state=InteractionState.ANSWERED,
        sla_breached=True,
    )
    session.add(recreated)
    await session.flush()

    assert await is_dismissed(session, chat.id, anchor), "решение не пережило пересборку"
    counts = await drill_counts(session, START, END, chat.id)
    assert counts[KIND_BREACH_REACTION] == 1, "учтено что-то кроме неснятого якоря"

    assert await restore(session, owner, chat.id, anchor)
    await session.flush()
    counts = await drill_counts(session, START, END, chat.id)
    assert counts[KIND_BREACH_REACTION] == 2, "отмена решения не вернула нарушение"


@requires_db
async def test_dismissal_clears_staff_speed_and_problem_chats(session):
    owner = await _owner(session)
    person = Staff(full_name="Ирина Тестова", normalized_name="ирина тестова")
    session.add(person)
    chat = Chat(tg_chat_id=-100660004, title="Скорость", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=START)

    breach = await _breach(
        session, chat, 1, first_reaction_staff_id=person.id
    )
    await dismiss(session, owner, chat.id, breach.opened_by_message_id)
    await session.flush()

    rows = await staff_speed(session, START, END)
    irina = next(row for row in rows if row["full_name"] == "Ирина Тестова")
    assert irina["breached"] == 0, "снятое нарушение вменяется сотруднику"

    problems = await problem_chats(session, START, END)
    assert all(
        row["breached"] == 0 for row in problems if row["title"] == "Скорость"
    ), "снятое нарушение держит чат в «проблемных»"


@requires_db
async def test_double_press_dismiss_is_idempotent(session, monkeypatch):
    """Второе нажатие прошло проверку до записи первого: «уже снято», а не IntegrityError
    с прерванной транзакцией обработчика."""
    from unittest.mock import AsyncMock

    from sqlalchemy import func

    from app.services import dismissals

    owner = await _owner(session)
    chat = Chat(tg_chat_id=-100660009, title="Двойное нажатие", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    breach = await _breach(session, chat, 1)

    assert await dismiss(session, owner, chat.id, breach.opened_by_message_id)
    await session.flush()
    monkeypatch.setattr(dismissals, "is_dismissed", AsyncMock(return_value=False))
    assert await dismissals.dismiss(session, owner, chat.id, breach.opened_by_message_id) is False

    decisions = await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "breach.dismissed")
    )
    assert decisions == 1, "проигравшее нажатие оставило след в журнале"
