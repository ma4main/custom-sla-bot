"""Напоминание о привязке: новый сотрудник ↔ учётки без привязки.

Новичка добавляют в бота до его первого сообщения в чатах — привязать
не к кому. Когда человек начинает писать и запись в справочнике появляется,
владельцу и админам приходит напоминание.
"""

from __future__ import annotations

from app.db.models import BotRole, BotUser, BotUserState, Staff
from app.services.staff import collect_link_nudge
from tests.conftest import requires_db

OWNER_TG = 930001
MANAGER_TG = 930002


def _owner() -> BotUser:
    return BotUser(
        tg_user_id=OWNER_TG, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
    )


def _manager(staff_id: int | None = None) -> BotUser:
    return BotUser(
        tg_user_id=MANAGER_TG,
        display_name="Новичок Тестов",
        role=BotRole.MANAGER,
        permissions={},
        state=BotUserState.ACTIVE,
        staff_id=staff_id,
    )


@requires_db
async def test_nudge_fires_once_and_names_both_sides(session):
    session.add_all([_owner(), _manager()])
    session.add(Staff(full_name="Ирина Новая", normalized_name="ирина новая"))
    await session.flush()

    payload = await collect_link_nudge(session)

    assert payload is not None, "повод есть — напоминание не собралось"
    text, recipients = payload
    assert "Ирина Новая" in text, "нет имени нового сотрудника"
    assert "Новичок Тестов" in text, "нет непривязанной учётки"
    assert OWNER_TG in recipients, "владелец не в получателях"
    assert MANAGER_TG not in recipients, "напоминание ушло самому новичку"

    # Дедуп: тот же сотрудник второй раз не поднимается.
    assert await collect_link_nudge(session) is None


@requires_db
async def test_no_unlinked_accounts_means_silence_and_no_backlog(session):
    """Без непривязанных учёток — тишина, и старое не всплывает позже."""
    session.add(_owner())
    session.add(Staff(full_name="Тихий Сотрудник", normalized_name="тихий сотрудник"))
    await session.flush()

    assert await collect_link_nudge(session) is None

    # Менеджер появился ПОЗЖЕ — про давно рассмотренного сотрудника
    # напоминание не поднимается: отметка сдвинута при первом проходе.
    session.add(_manager())
    await session.flush()
    assert await collect_link_nudge(session) is None


@requires_db
async def test_linked_manager_does_not_trigger(session):
    person = Staff(full_name="Связанная Анна", normalized_name="связанная анна")
    session.add(person)
    session.add(_owner())
    await session.flush()
    session.add(_manager(staff_id=person.id))
    session.add(Staff(full_name="Второй Новый", normalized_name="второй новый"))
    await session.flush()

    assert await collect_link_nudge(session) is None, (
        "привязанная учётка не повод для напоминания"
    )


@requires_db
async def test_names_are_html_escaped(session):
    """Напоминание уходит с parse_mode=HTML: «<» и «&» в именах экранируются."""
    session.add(_owner())
    session.add(
        BotUser(
            tg_user_id=MANAGER_TG,
            display_name="Лёва <Ко> & Сын",
            role=BotRole.MANAGER,
            permissions={},
            state=BotUserState.ACTIVE,
        )
    )
    session.add(Staff(full_name="Анна <b>", normalized_name="анна b"))
    await session.flush()

    payload = await collect_link_nudge(session)

    assert payload is not None
    text, _ = payload
    assert "Анна &lt;b&gt;" in text, "имя сотрудника не экранировано"
    assert "Лёва &lt;Ко&gt; &amp; Сын" in text, "имя учётки не экранировано"
