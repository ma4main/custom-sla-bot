"""Решение «не нарушение» действует везде, «последние 7 дней» — без сегодняшнего дня.

Снятое решением обращение не показывается ни в «Ответа так и нет», ни в
«Требует внимания» — а не только в срезах просрочек.
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
from app.services.report_drill import KIND_WAITING, drill_counts, drill_page
from app.services.report_lab import attention_now
from app.services.tracking import open_period
from tests.conftest import requires_db

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
START, END = NOW - timedelta(days=7), NOW + timedelta(days=1)
CFG_CAL = {
    "weekdays": [1, 2, 3, 4, 5],
    "start": "10:00",
    "end": "19:00",
    "timezone": "Europe/Moscow",
    "holidays": [],
}


def test_last7_ends_yesterday():
    from zoneinfo import ZoneInfo

    from app.bot.handlers.reports import period_bounds
    from app.config import get_settings

    start, end = period_bounds("last7")
    tz = ZoneInfo(get_settings().tz)
    today0 = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    assert end.astimezone(tz) == today0, "сегодняшний день не входит"
    assert (end - start) == timedelta(days=7), "ровно семь завершённых дней"


@requires_db
async def test_dismissed_live_case_leaves_waiting_and_attention(session):
    chat = Chat(tg_chat_id=-100995001, title="ВЕКТОР", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=START - timedelta(days=1))
    opened = NOW - timedelta(hours=3)
    message = Message(
        chat_id=chat.id, tg_message_id=1, tg_user_id=1,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT, text="Где акт?", char_count=8, sent_at=opened,
    )
    session.add(message)
    await session.flush()
    session.add(
        Interaction(
            chat_id=chat.id, opened_at=opened, opened_by_message_id=message.id,
            last_client_at=opened, client_messages=1, state=InteractionState.OPEN,
        )
    )
    await session.flush()

    assert (await drill_counts(session, START, END))[KIND_WAITING] == 1
    assert len(await attention_now(session, CFG_CAL, NOW, 30)) == 1

    owner = BotUser(tg_user_id=770700, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)
    session.add(owner)
    await session.flush()
    session.add(
        BreachDismissal(chat_id=chat.id, opened_by_message_id=message.id, dismissed_by=owner.id)
    )
    await session.flush()

    assert (await drill_counts(session, START, END))[KIND_WAITING] == 0, (
        "снятое решением обращение должно уйти из «Ответа так и нет»"
    )
    _, total = await drill_page(session, KIND_WAITING, START, END)
    assert total == 0
    assert await attention_now(session, CFG_CAL, NOW, 30) == [], (
        "…и из «Требует внимания»"
    )
