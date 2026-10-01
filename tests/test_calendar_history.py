"""История версий рабочего графика: прошлое не пересчитывается.

Обращение считается по графику, действовавшему в момент обращения: разбор
версий, запись версии при изменении настройки и пересборка эпизодов.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.calendar import CalendarHistory, business_seconds
from app.services.episodes import rebuild_interactions
from app.services.tracking import open_period
from tests.conftest import requires_db

MSK = timezone(timedelta(hours=3))

# Понедельник 24.08.2026 и среда 26.08.2026 — по обе стороны от смены.
MON_1650 = datetime(2026, 8, 24, 16, 50, tzinfo=MSK)
MON_1810 = datetime(2026, 8, 24, 18, 10, tzinfo=MSK)
WED_1650 = datetime(2026, 8, 26, 16, 50, tzinfo=MSK)
WED_1810 = datetime(2026, 8, 26, 18, 10, tzinfo=MSK)
SWITCH = datetime(2026, 8, 25, 10, 0, tzinfo=MSK)  # вторник, начало дня

DAY_19 = {
    "weekdays": [1, 2, 3, 4, 5],
    "start": "10:00",
    "end": "19:00",
    "timezone": "Europe/Moscow",
    "holidays": [],
}
VERSIONED = dict(
    DAY_19,
    end="17:00",
    since=SWITCH.isoformat(),
    history=[dict(DAY_19, since=None)],
)


# ── Разбор версий ────────────────────────────────────────────────

def test_calendar_at_picks_the_version_of_the_moment():
    history = CalendarHistory(VERSIONED)

    assert history.at(MON_1650)["end"] == "19:00", "до смены — прежний график"
    assert history.at(WED_1650)["end"] == "17:00", "после смены — новый"
    assert history.current["end"] == "17:00"
    # Ровно в момент смены действует уже новая версия.
    assert history.at(SWITCH)["end"] == "17:00"


def test_calendar_without_history_is_one_version():
    history = CalendarHistory(DAY_19)

    assert not history.changed()
    assert history.at(MON_1650)["end"] == "19:00"
    assert history.footnote(MON_1650, WED_1810) is None, "менять было нечего"


def test_business_seconds_differ_by_version():
    """Та же пара моментов, разные графики — разное рабочее время."""
    history = CalendarHistory(VERSIONED)

    assert business_seconds(MON_1650, MON_1810, history.at(MON_1650)) == 80 * 60
    assert business_seconds(WED_1650, WED_1810, history.at(WED_1650)) == 10 * 60


def test_footnote_names_both_schedules():
    note = CalendarHistory(VERSIONED).footnote(MON_1650, WED_1810)

    assert note is not None
    assert "10:00–19:00" in note and "10:00–17:00" in note
    assert "25.08 10:00" in note
    assert "не пересчитывается" in note


def test_footnote_is_silent_outside_the_change():
    """Период целиком после смены — сноска не нужна, график был один."""
    history = CalendarHistory(VERSIONED)

    assert history.footnote(WED_1650, WED_1810) is None


# ── Запись версии при изменении настройки ────────────────────────

@requires_db
async def test_changing_the_schedule_writes_a_version(session):
    from app.services.settings_store import get_section, section_extra, set_value

    await set_value(session, "work_calendar", "end", "17:00", actor_id=None, now=SWITCH)
    await session.flush()

    values = await get_section(session, "work_calendar")
    assert values["end"] == "17:00"
    assert values["since"] == SWITCH.isoformat()
    assert [entry["end"] for entry in values["history"]] == ["19:00"]

    # Второе изменение сдвигает в историю уже действующую версию.
    later = SWITCH + timedelta(days=7)
    await set_value(session, "work_calendar", "end", "18:00", actor_id=None, now=later)
    await session.flush()
    values = await get_section(session, "work_calendar")
    assert [entry["end"] for entry in values["history"]] == ["19:00", "17:00"]
    assert CalendarHistory(values).at(WED_1650)["end"] == "17:00"

    # История видна человеку на экране раздела — иначе непонятно,
    # почему прошлая неделя не поехала за настройкой.
    extra = section_extra("work_calendar", values)
    assert "История графика" in extra and "10:00–17:00" in extra


@requires_db
async def test_same_value_does_not_create_a_version(session):
    from app.services.settings_store import get_section, set_value

    await set_value(session, "work_calendar", "end", "19:00", actor_id=None, now=SWITCH)
    await session.flush()

    values = await get_section(session, "work_calendar")
    assert not values["history"], "значение то же — версии нет"


# ── Пересборка эпизодов ──────────────────────────────────────────

async def _case(session, tg_chat_id: int, title: str, asked: datetime, answered: datetime):
    chat = Chat(tg_chat_id=tg_chat_id, title=title, state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=asked - timedelta(days=1))
    session.add(
        Message(
            chat_id=chat.id,
            tg_message_id=1,
            transport_actor_kind=TransportActorKind.HUMAN_USER,
            business_side=BusinessSide.CLIENT,
            text="Когда будет готов акт сверки?",
            char_count=29,
            sent_at=asked,
        )
    )
    session.add(
        Message(
            chat_id=chat.id,
            tg_message_id=2,
            transport_actor_kind=TransportActorKind.HUMAN_USER,
            business_side=BusinessSide.COMPANY,
            text="Акт готов, направляю на подпись.",
            char_count=32,
            sent_at=answered,
        )
    )
    await session.flush()
    return chat


@requires_db
async def test_threshold_change_does_not_rewrite_past_breaches(session):
    """Подъём порога не стирает просрочки прошлого.

    Обращение отвечено с опозданием 35 минут при пороге 30 — просрочка.
    Порог подняли до часа сегодня: пересборка эпизодов не должна трогать
    закрытое прошлое.
    """
    from app.services.settings_store import set_value

    asked = MON_1650
    answered = asked + timedelta(minutes=35)
    chat = await _case(session, -100880011, "Порог", asked, answered)

    await rebuild_interactions(session, now=answered + timedelta(hours=2))
    await session.flush()
    before = await session.scalar(
        select(Interaction).where(Interaction.chat_id == chat.id)
    )
    assert before.sla_breached is True, "35 минут при пороге 30 — просрочка"

    await set_value(session, "alerts", "threshold_minutes", 60, actor_id=None, now=SWITCH)
    await session.flush()
    await rebuild_interactions(session, now=answered + timedelta(hours=2))
    await session.flush()

    after = await session.scalar(
        select(Interaction).where(Interaction.chat_id == chat.id)
    )
    assert after.sla_breached is True, (
        "просрочка отвеченного обращения изменилась от сегодняшней настройки"
    )


@requires_db
async def test_widening_the_window_does_not_revive_a_dead_case(session):
    """Расширение окна не оживляет уже закрытое «без ответа»."""
    from app.services.settings_store import set_value

    chat = await _case(session, -100880012, "Окно", MON_1650, MON_1650)
    # Убираем ответ: обращение должно остаться без реакции вовсе.
    await session.execute(
        delete(Message).where(
            Message.chat_id == chat.id,
            Message.business_side == BusinessSide.COMPANY,
        )
    )
    await session.flush()

    # Срок 17:20, окно сутки → мертво в среду 17:21.
    dead_at = MON_1650 + timedelta(days=1, hours=1)
    await rebuild_interactions(session, now=dead_at)
    await session.flush()
    assert (
        await session.scalar(
            select(Interaction.state).where(Interaction.chat_id == chat.id)
        )
    ) is InteractionState.ABANDONED

    await set_value(
        session, "episodes", "wait_reaction_hours", 100, actor_id=None, now=SWITCH
    )
    await session.flush()
    await rebuild_interactions(session, now=dead_at)
    await session.flush()

    assert (
        await session.scalar(
            select(Interaction.state).where(Interaction.chat_id == chat.id)
        )
    ) is InteractionState.ABANDONED, "кейс ожил от смены настройки задним числом"


@requires_db
async def test_history_is_not_recalculated_after_schedule_change(session):
    """Смена конца дня не переписывает просрочки прошлых дней.

    Обращение понедельника с 80 минутами ожидания и просрочкой не должно
    задним числом превратиться в 10 минут без нарушения.
    """
    from app.services.settings_store import set_value

    before = await _case(session, -100880001, "До смены", MON_1650, MON_1810)
    after = await _case(session, -100880002, "После смены", WED_1650, WED_1810)

    await set_value(session, "work_calendar", "end", "17:00", actor_id=None, now=SWITCH)
    await session.flush()

    await rebuild_interactions(session, now=WED_1810 + timedelta(days=1))
    await session.flush()

    old = await session.scalar(
        select(Interaction).where(Interaction.chat_id == before.id)
    )
    new = await session.scalar(
        select(Interaction).where(Interaction.chat_id == after.id)
    )

    # Понедельник — по графику до 19:00: 80 минут ожидания, срок 17:20 сорван.
    assert old.ttfr_business_seconds == 80 * 60
    assert old.sla_breached is True

    # Среда — по графику до 17:00: рабочего времени всего 10 минут, а срок
    # не помещался до конца дня и переехал на утро четверга — нарушения нет.
    assert new.ttfr_business_seconds == 10 * 60
    assert new.sla_breached is False
