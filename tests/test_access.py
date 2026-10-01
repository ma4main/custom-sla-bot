"""Роли и права: что нельзя выдать через интерфейс бота."""

import pytest

from app.db.models import BotRole, BotUser, BotUserState
from app.services.access import (
    ASSIGNABLE_ORDER,
    ASSIGNABLE_ROLES,
    ROLE_HINTS,
    ROLE_LABELS,
    STATE_LABELS,
    AccessError,
    Perm,
    ROLE_PRESETS,
    approve_user,
    change_role,
    create_invite,
    disable_user,
    effective_permissions,
    enable_user,
    promote_to_owner,
    role_label,
    state_label,
    transfer_ownership,
)


def user(role: BotRole, ident: int = 1, state: BotUserState = BotUserState.ACTIVE) -> BotUser:
    return BotUser(id=ident, tg_user_id=ident, role=role, permissions={}, state=state)


def test_privileged_roles_are_not_assignable():
    assert BotRole.OWNER not in ASSIGNABLE_ROLES
    assert BotRole.ADMIN in ASSIGNABLE_ROLES


async def test_owner_is_not_assignable_directly():
    """UI не рисует такую кнопку, но callback data — недоверенный ввод:
    закрыты все пути выдачи, а не только change_role.
    """
    owner = user(BotRole.OWNER)
    someone = user(BotRole.ADMIN, 4)
    pending = user(BotRole.MANAGER, 2, BotUserState.PENDING)
    with pytest.raises(AccessError):
        await change_role(None, owner, someone, BotRole.OWNER)
    with pytest.raises(AccessError):
        await approve_user(None, owner, pending, BotRole.OWNER)
    with pytest.raises(AccessError):
        await create_invite(None, owner, BotRole.OWNER)


# ═══════════════════════════════════════════════════════════════
# Роли наружу: только три, и все — по-русски
# ═══════════════════════════════════════════════════════════════


def test_assignable_order_matches_the_set():
    """Порядок кнопок задан списком, а не обходом множества.

    Множество порядок не хранит: кнопки переставлялись бы между запусками.
    """
    assert set(ASSIGNABLE_ORDER) == ASSIGNABLE_ROLES
    assert len(ASSIGNABLE_ORDER) == len(ASSIGNABLE_ROLES), "дубль в списке порядка"


def test_every_role_and_state_speaks_russian():
    """Ни одна роль и ни одно состояние не должны утечь наружу как есть."""
    for role in BotRole:
        assert role in ROLE_LABELS, role
        assert role in ROLE_HINTS, role
        assert role_label(role) != role.value, role

    for state in BotUserState:
        assert state in STATE_LABELS, state
        assert state_label(state) != state.value, state


def test_admin_is_owner_minus_ownership():
    """Админ = владелец минус владение."""
    admin = effective_permissions(user(BotRole.ADMIN))
    owner = effective_permissions(user(BotRole.OWNER, 2))

    assert owner - admin == {Perm.OWNERSHIP_TRANSFER}, (
        "разница ролей должна быть ровно в передаче владения"
    )
    assert Perm.USER_MANAGE in admin and Perm.SYSTEM_SETTINGS in admin


async def test_admin_cannot_touch_an_owner():
    """«Всё, кроме владения» — это и неприкосновенность владельцев.

    Иначе админ с управлением пользователями понизил бы владельца
    и фактически перехватил бота.
    """
    admin = user(BotRole.ADMIN)
    boss = user(BotRole.OWNER, 2)

    with pytest.raises(AccessError):
        await change_role(None, admin, boss, BotRole.MANAGER)
    with pytest.raises(AccessError):
        await disable_user(None, admin, boss)
    with pytest.raises(AccessError):
        await promote_to_owner(None, admin, user(BotRole.MANAGER, 3))
    with pytest.raises(AccessError):
        await transfer_ownership(None, admin, user(BotRole.MANAGER, 3))


def test_manager_label_does_not_promise_authority():
    """«Руководитель» обещал начальника, а прав — только свои показатели.

    Плюс «менеджер» уже занят в «Сотрудниках» наблюдаемой ролью (кто
    передаёт специалисту): одно слово в двух смыслах путало бы оба экрана.
    """
    assert ROLE_PRESETS[BotRole.MANAGER] == {Perm.REPORT_SELF, Perm.ALERT_RECEIVE_SELF}
    assert "уковод" not in ROLE_LABELS[BotRole.MANAGER]


# ═══════════════════════════════════════════════════════════════
# Возврат отключённого пользователя
# ═══════════════════════════════════════════════════════════════


class _Journal:
    """Подставная сессия: запоминает добавленные записи журнала."""

    def __init__(self) -> None:
        self.rows: list = []

    def add(self, row) -> None:
        self.rows.append(row)


async def test_enable_returns_disabled_user_with_previous_role():
    session = _Journal()
    person = user(BotRole.ADMIN, 2, BotUserState.DISABLED)

    await enable_user(session, user(BotRole.OWNER), person)
    assert person.state is BotUserState.ACTIVE
    assert person.role is BotRole.ADMIN, "роль при возврате сменилась"
    [entry] = session.rows
    assert entry.action == "user.enabled" and entry.object_id == "2"
    assert entry.payload == {"role": "admin"}


async def test_enable_follows_disable_permissions():
    """Включает тот, кто может отключать: владельца — только владелец, заявка — не сюда."""
    with pytest.raises(AccessError):
        await enable_user(_Journal(), user(BotRole.MANAGER), user(BotRole.MANAGER, 2, BotUserState.DISABLED))
    with pytest.raises(AccessError):
        await enable_user(_Journal(), user(BotRole.ADMIN), user(BotRole.OWNER, 2, BotUserState.DISABLED))
    with pytest.raises(AccessError):
        await enable_user(_Journal(), user(BotRole.OWNER), user(BotRole.MANAGER, 2, BotUserState.PENDING))
    with pytest.raises(AccessError):
        await enable_user(_Journal(), user(BotRole.OWNER), user(BotRole.MANAGER, 2))

    boss = user(BotRole.OWNER, 2, BotUserState.DISABLED)
    await enable_user(_Journal(), user(BotRole.OWNER), boss)
    assert boss.state is BotUserState.ACTIVE and boss.role is BotRole.OWNER
