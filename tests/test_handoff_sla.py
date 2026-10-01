"""Второй слой SLA: помощник передал вопрос специалисту — тот отвечает к тому же
времени следующего рабочего дня. Второй слой открывает только явная передача."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    Classification,
    Interaction,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.calendar import same_time_next_workday
from app.services.episodes import rebuild_interactions
from app.services.tracking import open_period
from app.services.verdicts import SOURCE_MODEL
from tests.conftest import requires_db

# Рабочий календарь по умолчанию: пн–пт 10:00–19:00 МСК (07:00–16:00 UTC).
MONDAY_11 = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)  # пн 11:00 МСК
FRIDAY_17 = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)  # пт 17:00 МСК
CALENDAR = {
    "weekdays": [1, 2, 3, 4, 5],
    "start": "10:00",
    "end": "19:00",
    "timezone": "Europe/Moscow",
    "holidays": [],
}
DAY = 24 * 3600


# ═══════════════════════════════════════════════════════════════
# Срок: то же время следующего рабочего дня
# ═══════════════════════════════════════════════════════════════


def test_deadline_is_same_time_next_day():
    """Передали в понедельник в 11:00 — специалист отвечает до вторника 11:00."""
    deadline = same_time_next_workday(MONDAY_11, DAY, CALENDAR)
    assert deadline == MONDAY_11 + timedelta(days=1)


def test_deadline_jumps_over_the_weekend():
    """Передали в пятницу в 17:00 — крайний срок понедельник 17:00.

    Календарные сутки дали бы субботу, когда никто не работает.
    """
    deadline = same_time_next_workday(FRIDAY_17, DAY, CALENDAR)

    moscow = deadline.astimezone(timezone(timedelta(hours=3)))
    assert moscow.weekday() == 0, "срок должен попасть на понедельник"
    assert (moscow.hour, moscow.minute) == (17, 0)


def test_deadline_is_clamped_into_working_hours():
    """Срок не может прийтись на время, когда никто не работает."""
    # Передача в 22:00 МСК: сутки спустя — тоже 22:00, вне окна.
    late = datetime(2026, 8, 24, 19, 0, tzinfo=timezone.utc)

    deadline = same_time_next_workday(late, DAY, CALENDAR)

    moscow = deadline.astimezone(timezone(timedelta(hours=3)))
    assert (moscow.hour, moscow.minute) == (19, 0), "срок прижат к концу дня"


def test_holidays_push_the_deadline_further():
    calendar = {**CALENDAR, "holidays": ["2026-08-25"]}

    deadline = same_time_next_workday(MONDAY_11, DAY, calendar)

    assert deadline == MONDAY_11 + timedelta(days=2), "праздник должен сдвинуть срок"


# ═══════════════════════════════════════════════════════════════
# Движок: передача открывает второй слой, её отсутствие — закрывает эпизод
# ═══════════════════════════════════════════════════════════════


async def _episode(session, *, company_label: str, tg_chat_id: int) -> Chat:
    """Клиент спросил, помощник ответил сообщением с заданной меткой."""
    chat = Chat(tg_chat_id=tg_chat_id, title="Второй слой", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=MONDAY_11 - timedelta(days=1))

    client = Message(
        chat_id=chat.id,
        tg_message_id=1,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Подскажите по акту сверки за июль",
        char_count=33,
        sent_at=MONDAY_11,
    )
    company = Message(
        chat_id=chat.id,
        tg_message_id=2,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY,
        text="Добрый день, передала запрос бухгалтеру",
        char_count=38,
        sent_at=MONDAY_11 + timedelta(minutes=5),
    )
    session.add_all([client, company])
    await session.flush()

    model = get_settings().ai_model
    session.add(
        Classification(
            message_id=client.id,
            model=model,
            prompt_version=2,
            source=SOURCE_MODEL,
            label="question",
            requires_response=True,
        )
    )
    session.add(
        Classification(
            message_id=company.id,
            model=model,
            prompt_version=2,
            source=SOURCE_MODEL,
            label=company_label,
            is_substantive=company_label == "substantive",
        )
    )
    await session.flush()
    return chat


async def _interaction(session, chat_id: int) -> Interaction:
    return await session.scalar(
        select(Interaction).where(Interaction.chat_id == chat_id)
    )


@requires_db
async def test_handoff_opens_the_second_layer(session, monkeypatch):
    """«Передала бухгалтеру» — обращение ждёт специалиста, а не закрыто."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _episode(session, company_label="handoff", tg_chat_id=-100444001)

    await rebuild_interactions(session, now=MONDAY_11 + timedelta(hours=1))
    await session.flush()

    episode = await _interaction(session, chat.id)
    assert episode.state is InteractionState.REACTED
    assert episode.handoff_at == MONDAY_11 + timedelta(minutes=5)


@requires_db
async def test_no_handoff_closes_the_episode(session, monkeypatch):
    """Передачи не было — значит помощник ответил сам, ждать некого."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _episode(session, company_label="ack", tg_chat_id=-100444002)

    await rebuild_interactions(session, now=MONDAY_11 + timedelta(hours=1))
    await session.flush()

    episode = await _interaction(session, chat.id)
    assert episode.state is InteractionState.ANSWERED
    assert episode.handoff_at is None, "передачи не было — второго слоя нет"


@requires_db
async def test_substantive_answer_closes_without_handoff(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _episode(session, company_label="substantive", tg_chat_id=-100444003)

    await rebuild_interactions(session, now=MONDAY_11 + timedelta(hours=1))
    await session.flush()

    episode = await _interaction(session, chat.id)
    assert episode.state is InteractionState.ANSWERED
    assert episode.substantive_at is not None
    assert episode.substantive_breached is None, (
        "передачи не было — нарушать было нечего, второго слоя не существует"
    )


@requires_db
async def test_specialist_answer_in_time_is_not_a_breach(session, monkeypatch):
    """Бухгалтер ответил на следующее утро — в срок."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _episode(session, company_label="handoff", tg_chat_id=-100444004)

    answer = Message(
        chat_id=chat.id,
        tg_message_id=3,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY,
        text="Акт сверки во вложении, расхождений нет",
        char_count=38,
        sent_at=MONDAY_11 + timedelta(hours=23),
    )
    session.add(answer)
    await session.flush()
    session.add(
        Classification(
            message_id=answer.id,
            model=get_settings().ai_model,
            prompt_version=2,
            source=SOURCE_MODEL,
            label="substantive",
            is_substantive=True,
        )
    )
    await session.flush()

    await rebuild_interactions(session, now=MONDAY_11 + timedelta(days=2))
    await session.flush()

    episode = await _interaction(session, chat.id)
    assert episode.state is InteractionState.ANSWERED
    assert episode.substantive_breached is False


@requires_db
async def test_waiting_for_specialist_is_not_abandoned_early(session, monkeypatch):
    """Пока идёт срок специалиста, обращение не снимается по общему пределу.

    Окно ожидания отсчитывается от срока, а не от обращения клиента.
    Иначе оно закрывало бы такое обращение раньше, чем срок специалиста
    наступит, и второй алерт не получал бы права сработать никогда.
    """
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _episode(session, company_label="handoff", tg_chat_id=-100444006)

    # Сутки спустя: срок специалиста наступает только назавтра в 11:05.
    await rebuild_interactions(session, now=MONDAY_11 + timedelta(hours=23))
    await session.flush()

    episode = await _interaction(session, chat.id)
    assert episode.state is InteractionState.REACTED, (
        "обращение сняли по общему пределу, пока специалист ещё в своём сроке"
    )


@requires_db
async def test_abandoned_only_well_after_the_specialist_deadline(session, monkeypatch):
    """Срок специалиста прошёл и ещё неделя — вот теперь снимаем."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _episode(session, company_label="handoff", tg_chat_id=-100444007)

    await rebuild_interactions(session, now=MONDAY_11 + timedelta(days=9))
    await session.flush()

    episode = await _interaction(session, chat.id)
    assert episode.state is InteractionState.ABANDONED


@requires_db
async def test_specialist_answer_too_late_is_a_breach(session, monkeypatch):
    """Ответ через два дня — срок специалиста нарушен."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _episode(session, company_label="handoff", tg_chat_id=-100444005)

    answer = Message(
        chat_id=chat.id,
        tg_message_id=3,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY,
        text="Акт сверки во вложении",
        char_count=22,
        sent_at=MONDAY_11 + timedelta(days=2),
    )
    session.add(answer)
    await session.flush()
    session.add(
        Classification(
            message_id=answer.id,
            model=get_settings().ai_model,
            prompt_version=2,
            source=SOURCE_MODEL,
            label="substantive",
            is_substantive=True,
        )
    )
    await session.flush()

    await rebuild_interactions(session, now=MONDAY_11 + timedelta(days=3))
    await session.flush()

    episode = await _interaction(session, chat.id)
    assert episode.substantive_breached is True
