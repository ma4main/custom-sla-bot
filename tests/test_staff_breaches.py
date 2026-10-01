"""Просрочки по сотрудникам и чатам в сводном отчёте.

Обе ступени считаются тому, кто отвечал; снятые решением — нет;
обращение без ответившего никому не приписывается.
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
    Staff,
    TransportActorKind,
)
from app.services.report_lab import staff_breaches
from app.services.tracking import open_period
from tests.conftest import requires_db

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
START, END = NOW - timedelta(days=7), NOW + timedelta(days=1)


async def _chat(session, tg_id: int, title: str) -> Chat:
    chat = Chat(tg_chat_id=tg_id, title=title, state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=START - timedelta(days=1))
    return chat


async def _interaction(session, chat: Chat, n: int, **fields) -> Interaction:
    opened = NOW - timedelta(hours=n)
    message = Message(
        chat_id=chat.id, tg_message_id=n, tg_user_id=1,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT, text="?", char_count=1, sent_at=opened,
    )
    session.add(message)
    await session.flush()
    interaction = Interaction(
        chat_id=chat.id, opened_at=opened, opened_by_message_id=message.id,
        last_client_at=opened, client_messages=1, state=InteractionState.ANSWERED,
        **fields,
    )
    session.add(interaction)
    await session.flush()
    return interaction


@requires_db
async def test_breaches_grouped_by_staff_and_chat(session):
    irina = Staff(full_name="Ирина Соколова", normalized_name="ирина соколова")
    nina = Staff(full_name="Нина Козлова", normalized_name="нина козлова")
    session.add_all([irina, nina])
    await session.flush()
    sigma = await _chat(session, -100990001, "Сигма")
    vector = await _chat(session, -100990002, "ООО Вектор")

    # Ирина: две просрочки реакции в Сигме, одна — ответ специалиста в ООО Вектор.
    await _interaction(session, sigma, 1, sla_breached=True, first_reaction_staff_id=irina.id)
    await _interaction(session, sigma, 2, sla_breached=True, first_reaction_staff_id=irina.id)
    await _interaction(
        session, vector, 3, substantive_breached=True, substantive_staff_id=irina.id
    )
    # Нина: просрочка есть, но снята решением руководителя — не считается.
    owner = BotUser(tg_user_id=770600, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)
    session.add(owner)
    await session.flush()
    dismissed = await _interaction(
        session, vector, 4, sla_breached=True, first_reaction_staff_id=nina.id
    )
    session.add(
        BreachDismissal(
            chat_id=vector.id, opened_by_message_id=dismissed.opened_by_message_id,
            dismissed_by=owner.id,
        )
    )
    # Никто не ответил — никому не приписывается.
    await _interaction(session, vector, 5, sla_breached=True)
    # Ирина вела ещё одно обращение без просрочки — знаменатель доли.
    await _interaction(session, sigma, 6, first_reaction_staff_id=irina.id)
    # Обе ступени сорваны одним человеком — обращение считается ОДИН раз.
    await _interaction(
        session, sigma, 7,
        sla_breached=True, first_reaction_staff_id=irina.id,
        substantive_breached=True, substantive_staff_id=irina.id,
    )
    await session.flush()

    result = await staff_breaches(session, START, END)

    irina_hit = result[irina.id]
    assert irina_hit["count"] == 4, "4 обращения с просрочкой, не 5 ступеней"
    assert irina_hit["handled"] == 5
    assert irina_hit["share"] == 80
    assert irina_hit["chats"] == {"Сигма": 3, "ООО Вектор": 1}
    assert (irina_hit["reaction"], irina_hit["specialist"]) == (3, 2), (
        "разбивка по ступеням: 3 реакции, 2 специалиста (одно обращение — обе)"
    )
    assert result[nina.id]["count"] == 0, "снятое решением — не просрочка"
    assert result[nina.id]["handled"] == 1, "…но обращение она вела"
    assert set(result) == {irina.id, nina.id}, "безымянная просрочка никому не приписана"


@requires_db
async def test_breach_items_carry_details_for_drilldown(session):
    """Раскрывашка в HTML: каждая просрочка — чат, когда, ступень."""
    irina = Staff(full_name="Ирина Соколова", normalized_name="ирина соколова")
    session.add(irina)
    await session.flush()
    sigma = await _chat(session, -100990011, "Сигма")
    reacted = NOW - timedelta(hours=1, minutes=5)
    await _interaction(
        session, sigma, 1,
        sla_breached=True, first_reaction_staff_id=irina.id,
        first_reaction_at=reacted, ttfr_business_seconds=55 * 60,
    )
    await session.flush()

    hit = (await staff_breaches(session, START, END))[irina.id]
    assert len(hit["items"]) == hit["count"] == 1
    item = hit["items"][0]
    assert item["kind"] == "reaction" and item["title"] == "Сигма"
    assert item["until"] == reacted and item["business_delay"] == 55 * 60
