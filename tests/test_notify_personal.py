"""«Уведомления в личку»: владелец получает алерты и рассылки только
в группе, если сам так решил.

Два правила:
  1. адресаты алертов/рассылок — по праву И по флагу; системные
     уведомления флага не видят;
  2. выключить личку нельзя, пока группа не принимает и алерты, и рассылки —
     иначе уведомления ушли бы в никуда.
"""

from app.db.models import BotRole, BotUser, BotUserState
from app.services.access import (
    Perm,
    notification_recipients,
    personal_alert_recipients,
    personal_mute_blocker,
)
from tests.conftest import requires_db


def test_mute_blocker_matrix():
    on = {"to_group": True}
    off = {"to_group": False}
    assert personal_mute_blocker(on, on, -100123) is None
    assert "Группа уведомлений не задана" in personal_mute_blocker(on, on, None)
    assert "Алерты в группу" in personal_mute_blocker(off, on, -100123)
    assert "Слать и в группу уведомлений" in personal_mute_blocker(on, off, -100123)


@requires_db
async def test_recipients_respect_the_flag(session):
    quiet = BotUser(
        tg_user_id=770501,
        role=BotRole.OWNER,
        permissions={},
        state=BotUserState.ACTIVE,
        notify_personal=False,
    )
    loud = BotUser(
        tg_user_id=770502, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
    )
    session.add_all([quiet, loud])
    await session.flush()

    personal = {u.tg_user_id for u in await personal_alert_recipients(session)}
    assert 770502 in personal
    assert 770501 not in personal, "выключенная личка не получает алерты и рассылки"

    # Системные уведомления — по праву, флаг их не касается.
    system = {
        u.tg_user_id for u in await notification_recipients(session, Perm.ALERT_RECEIVE_ALL)
    }
    assert {770501, 770502} <= system
