"""Замолчавшее обращение закрывается по окну и без новых сообщений.

Окно КАЛЕНДАРНОЕ (сутки на реакцию, неделя на специалиста), но отсчитывается
от СРОКА, а не от сообщения клиента, — ночь, выходные и праздники его не съедают.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.episodes import rebuild_interactions
from app.services.tracking import open_period
from tests.conftest import requires_db

# Понедельник. Рабочий календарь по умолчанию — пн–пт 10:00–19:00 МСК,
# то есть 07:00–16:00 UTC. Окно ожидания реакции — сутки после срока.
#
# Пороги и окна версионируются: правка настройки действует С МОМЕНТА
# правки, поэтому в тестах момент задаётся явно и заведомо раньше
# тестовых данных — иначе настройка к ним просто не применится.
RULES_SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
MONDAY_1030 = datetime(2026, 8, 24, 7, 30, tzinfo=timezone.utc)
MONDAY_1850 = datetime(2026, 8, 24, 15, 50, tzinfo=timezone.utc)


async def _chat_with_unanswered_request(session, *, at: datetime) -> Chat:
    """Клиент спросил и замолчал; компания не отвечала вовсе."""
    chat = Chat(tg_chat_id=-100777101, title="Молчание", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=at - timedelta(days=1))

    session.add(
        Message(
            chat_id=chat.id,
            tg_message_id=1,
            transport_actor_kind=TransportActorKind.HUMAN_USER,
            business_side=BusinessSide.CLIENT,
            text="Подскажите, пожалуйста, когда будет готов акт сверки?",
            char_count=53,
            sent_at=at,
        )
    )
    await session.flush()
    return chat


async def _state(session, chat_id: int) -> InteractionState | None:
    return await session.scalar(
        select(Interaction.state).where(Interaction.chat_id == chat_id)
    )


@requires_db
async def test_open_episode_survives_while_within_window(session):
    """Полдня после срока — окно (сутки) ещё не вышло."""
    chat = await _chat_with_unanswered_request(session, at=MONDAY_1030)

    # 18:00 того же понедельника: срок реакции был в 11:00, сутки не прошли.
    await rebuild_interactions(session, now=MONDAY_1030 + timedelta(hours=7, minutes=30))
    await session.flush()

    assert await _state(session, chat.id) is InteractionState.OPEN


@requires_db
async def test_silent_episode_is_abandoned_without_new_messages(session):
    """Новых сообщений нет — эпизод всё равно закрывается по окну."""
    chat = await _chat_with_unanswered_request(session, at=MONDAY_1030)

    # Вторник, 12:00 МСК: срок был в понедельник 11:00, сутки после него
    # истекли во вторник в 11:00.
    await rebuild_interactions(
        session, now=MONDAY_1030 + timedelta(days=1, hours=1, minutes=30)
    )
    await session.flush()

    assert await _state(session, chat.id) is InteractionState.ABANDONED


@requires_db
async def test_window_counts_from_the_deadline_not_from_the_message(session):
    """Вечернее обращение не сгорает за ночь: сутки идут от СРОКА.

    Клиент написал в 18:50 при дне до 19:00 — полчаса до конца дня не
    помещаются, срок реакции переезжает на утро следующего рабочего дня.
    Считай мы сутки от сообщения — обращение закрылось бы «без ответа»
    раньше собственного срока, и алерт по нему не ушёл бы никогда.
    """
    chat = await _chat_with_unanswered_request(session, at=MONDAY_1850)

    # Вторник, 19:50 МСК — сутки с сообщения давно прошли, но срок
    # наступил только в 10:30 утра вторника: окно до среды 10:30.
    await rebuild_interactions(session, now=MONDAY_1850 + timedelta(hours=25))
    await session.flush()

    assert await _state(session, chat.id) is InteractionState.OPEN

    # А ещё через сутки — закрывается.
    await rebuild_interactions(session, now=MONDAY_1850 + timedelta(days=2))
    await session.flush()

    assert await _state(session, chat.id) is InteractionState.ABANDONED


@requires_db
async def test_window_starts_when_the_company_is_back_at_work(session):
    """Срок в нерабочее время не сжигает обращение до первого рабочего тика.

    Порог длиннее рабочего дня уводит срок в аварийную ветку
    `response_deadline`: обращение пятницы получает срок в субботу.
    Алерты шлются только в рабочее время — отсчитывай окно от такого
    срока, обращение умирало бы в воскресенье, и алерт по нему не ушёл бы
    никогда.
    """
    from app.services.settings_store import set_value

    # Пятница 28.08.2026, 12:00 МСК (09:00 UTC), день 10:00–19:00.
    friday_1200 = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
    await set_value(
        session, "alerts", "threshold_minutes", 600, actor_id=None, now=RULES_SINCE
    )
    await session.flush()
    chat = await _chat_with_unanswered_request(session, at=friday_1200)

    # Срок — суббота 22:00; окно обязано пойти с понедельника 10:00.
    monday_1000 = datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc)
    await rebuild_interactions(session, now=monday_1000 + timedelta(minutes=1))
    await session.flush()
    assert await _state(session, chat.id) is InteractionState.OPEN, (
        "снято до первого рабочего тика после срока — алерт мёртв"
    )

    await rebuild_interactions(session, now=monday_1000 + timedelta(days=1, hours=1))
    await session.flush()
    assert await _state(session, chat.id) is InteractionState.ABANDONED


@requires_db
async def test_limit_never_kills_an_alert_whatever_the_settings(session):
    """Настройкой нельзя сломать алерты: окно идёт от последнего из сроков.

    Окно в час при пороге реакции в два часа закрывало бы обращение раньше,
    чем алерт получит право сработать, — и алерт молча умирал бы.
    """
    from app.services.settings_store import set_value

    chat = await _chat_with_unanswered_request(session, at=MONDAY_1030)
    await set_value(
        session, "episodes", "wait_reaction_hours", 1, actor_id=None, now=RULES_SINCE
    )
    await set_value(
        session, "alerts", "threshold_minutes", 120, actor_id=None, now=RULES_SINCE
    )
    await session.flush()

    # Полтора часа спустя: окно (1 ч) кончилось бы, считай мы его
    # от сообщения, — но срок реакции (2 ч) ещё даже не наступил.
    await rebuild_interactions(session, now=MONDAY_1030 + timedelta(hours=1, minutes=30))
    await session.flush()

    assert await _state(session, chat.id) is InteractionState.OPEN, (
        "обращение сняли до наступления срока реакции — алерт мёртв"
    )
