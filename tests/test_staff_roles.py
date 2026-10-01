"""Наблюдаемые роли: менеджер передаёт, специалист закрывает передачи.

Списков ролей нет: роль выводится из фактов, которые
движок пишет на каждом эпизоде. Ручное назначение автоматика не трогает.
"""

from datetime import datetime, timedelta, timezone

from app.db.models import (
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
from app.services.staff import normalize_name
from app.services.staff_roles import (
    ROLE_MANAGER,
    ROLE_SPECIALIST,
    SOURCE_AUTO,
    SOURCE_MANUAL,
    refresh_observed_roles,
    set_manual_role,
)
from tests.conftest import requires_db

NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


def _staff(name: str) -> Staff:
    return Staff(full_name=name, normalized_name=normalize_name(name))


async def _handoff_episode(session, chat, n: int, manager: Staff, specialist: Staff) -> None:
    """Эпизод «менеджер передал — специалист закрыл»."""
    opened_at = NOW - timedelta(hours=n + 1)
    opener = Message(
        chat_id=chat.id,
        tg_message_id=n * 10 + 1,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Вопрос по отчётности",
        char_count=19,
        sent_at=opened_at,
    )
    session.add(opener)
    await session.flush()
    session.add(
        Interaction(
            chat_id=chat.id,
            opened_at=opened_at,
            opened_by_message_id=opener.id,
            last_client_at=opened_at,
            client_messages=1,
            state=InteractionState.ANSWERED,
            handoff_at=opened_at + timedelta(minutes=5),
            handoff_staff_id=manager.id,
            substantive_at=opened_at + timedelta(minutes=50),
            substantive_staff_id=specialist.id,
        )
    )


@requires_db
async def test_roles_observed_from_handoffs(session):
    chat = Chat(tg_chat_id=-100999020, title="Роли", state=ChatState.TRACKED)
    manager = _staff("Ирина Соколова")
    specialist = _staff("Роман Зайцев")
    session.add_all([chat, manager, specialist])
    await session.flush()

    # Одна передача — случайность: ролей ещё нет.
    await _handoff_episode(session, chat, 1, manager, specialist)
    await refresh_observed_roles(session)
    assert manager.role is None and specialist.role is None

    # Вторая — уже почерк: роли назначены автоматически.
    await _handoff_episode(session, chat, 2, manager, specialist)
    changed = await refresh_observed_roles(session)
    assert changed == 2
    assert manager.role == ROLE_MANAGER and manager.role_source == SOURCE_AUTO
    assert specialist.role == ROLE_SPECIALIST and specialist.role_source == SOURCE_AUTO


@requires_db
async def test_manual_role_survives_refresh(session):
    chat = Chat(tg_chat_id=-100999021, title="Роли", state=ChatState.TRACKED)
    manager = _staff("Ирина Соколова")
    specialist = _staff("Роман Зайцев")
    owner = BotUser(
        tg_user_id=910401, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
    )
    session.add_all([chat, manager, specialist, owner])
    await session.flush()

    for n in (1, 2):
        await _handoff_episode(session, chat, n, manager, specialist)

    # Человек сказал «Ирина — специалист»: наблюдения это не перебивают.
    await set_manual_role(session, owner, manager, ROLE_SPECIALIST)
    await refresh_observed_roles(session)
    assert manager.role == ROLE_SPECIALIST and manager.role_source == SOURCE_MANUAL


@requires_db
async def test_returning_to_auto_recomputes_immediately(session):
    chat = Chat(tg_chat_id=-100999022, title="Роли", state=ChatState.TRACKED)
    manager = _staff("Ирина Соколова")
    specialist = _staff("Роман Зайцев")
    owner = BotUser(
        tg_user_id=910402, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
    )
    session.add_all([chat, manager, specialist, owner])
    await session.flush()
    for n in (1, 2):
        await _handoff_episode(session, chat, n, manager, specialist)

    await set_manual_role(session, owner, manager, ROLE_SPECIALIST)
    # Вернули автоопределение — роль пересчиталась сразу, не ночным проходом.
    await set_manual_role(session, owner, manager, None)
    assert manager.role == ROLE_MANAGER and manager.role_source == SOURCE_AUTO


@requires_db
async def test_self_closed_handoff_is_not_specialist_signal(session):
    """Сам передал — сам закрыл: специалистом от этого не становятся."""
    chat = Chat(tg_chat_id=-100999023, title="Роли", state=ChatState.TRACKED)
    person = _staff("Ирина Соколова")
    session.add_all([chat, person])
    await session.flush()

    for n in (1, 2, 3):
        await _handoff_episode(session, chat, n, person, person)
    await refresh_observed_roles(session)
    # Передавал — менеджер; «закрытия» своих же передач в специалисты не ведут.
    assert person.role == ROLE_MANAGER
