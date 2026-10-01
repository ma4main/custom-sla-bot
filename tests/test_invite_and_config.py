"""Погашение приглашения."""

from sqlalchemy import select, text

from app.db.models import BotRole, BotUser, BotUserState, InviteCode
from app.services.access import _apply_invite, create_invite, hash_invite_code
from tests.conftest import requires_db


def _pending() -> BotUser:
    return BotUser(
        tg_user_id=555001, role=BotRole.MANAGER, permissions={}, state=BotUserState.PENDING
    )


@requires_db
async def test_invite_cannot_grant_owner(session):
    """Приглашение на невыдаваемую роль, записанное в обход create_invite, не гасится."""
    session.add(InviteCode(code_hash=hash_invite_code("ownercode"), role=BotRole.OWNER, created_by=None))
    user = _pending()
    session.add(user)
    await session.flush()

    assert await _apply_invite(session, user, "ownercode") is False
    assert user.role is BotRole.MANAGER
    assert user.state is BotUserState.PENDING
    unused = await session.scalar(select(InviteCode).where(InviteCode.code_hash == hash_invite_code("ownercode")))
    assert unused.used_at is None, "код погашен, хотя роль выдавать нельзя"


@requires_db
async def test_normal_invite_activates_user(session):
    session.add(InviteCode(code_hash=hash_invite_code("admincode"), role=BotRole.ADMIN, created_by=None))
    user = _pending()
    session.add(user)
    await session.flush()

    assert await _apply_invite(session, user, "admincode") is True
    assert user.role is BotRole.ADMIN
    assert user.state is BotUserState.ACTIVE


@requires_db
async def test_invite_code_is_never_stored_in_plaintext(session):
    """В базе — только отпечаток кода.

    Дамп базы, лог запроса или чужой взгляд в таблицу не должны давать
    готовый пропуск с ролью.
    """
    owner = BotUser(
        tg_user_id=555002, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
    )
    session.add(owner)
    await session.flush()

    invite, code = await create_invite(session, owner, BotRole.ADMIN)
    await session.flush()

    assert code, "код должен возвращаться вызывающему — показать его больше негде"
    assert invite.code_hash == hash_invite_code(code) != code

    # Проверяем всю строку целиком, а не одно поле: код не должен оказаться
    # ни в одной колонке, включая те, что появятся позже.
    row = (
        await session.execute(
            text("SELECT * FROM invite_code WHERE id = :id"), {"id": invite.id}
        )
    ).mappings().one()
    assert code not in " ".join(str(value) for value in row.values())

    # И при этом код продолжает работать.
    user = _pending()
    session.add(user)
    await session.flush()
    assert await _apply_invite(session, user, code) is True
    assert user.role is BotRole.ADMIN



@requires_db
async def test_invite_expires_in_48_hours(session):
    """Бессрочных приглашений интерфейс не выпускает."""
    from datetime import datetime, timedelta, timezone

    from app.services.access import INVITE_TTL

    owner = BotUser(
        tg_user_id=555003, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
    )
    session.add(owner)
    await session.flush()

    before = datetime.now(timezone.utc)
    invite, code = await create_invite(session, owner, BotRole.MANAGER)
    await session.flush()
    assert INVITE_TTL == timedelta(hours=48)
    assert invite.expires_at is not None, "приглашение без срока — бессрочный пропуск"
    assert timedelta(hours=47, minutes=59) < invite.expires_at - before < INVITE_TTL + timedelta(minutes=1)

    # Просроченное не гасится, даже если код верный.
    invite.expires_at = before - timedelta(minutes=1)
    await session.flush()
    user = _pending()
    session.add(user)
    await session.flush()
    assert await _apply_invite(session, user, code) is False
