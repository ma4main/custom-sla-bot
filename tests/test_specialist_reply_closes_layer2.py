"""Второй слой закрывается фактом выхода специалиста на связь.

Достаточно, что специалист увидел и принял вопрос («посмотрю сегодня
и отпишусь», метка ack) или сам ответил по существу. Специалист — тот, кто
пишет после передачи и не является передавшим помощником. «Принято» от
самого помощника после его же передачи вопрос не закрывает: бухгалтер
на связь ещё не выходил.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db.models import (
    Attribution,
    BusinessSide,
    Chat,
    ChatState,
    Classification,
    Interaction,
    InteractionState,
    Message,
    Staff,
    TransportActorKind,
)
from app.services.episodes import rebuild_interactions
from app.services.tracking import open_period
from tests.conftest import requires_db

# Понедельник, 11:19 МСК — рабочее время при календаре по умолчанию.
T0 = datetime(2026, 9, 7, 8, 19, tzinfo=timezone.utc)


async def _chat(session, tg_chat_id: int) -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title="ИП Смирнов", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=T0 - timedelta(days=1))
    return chat


async def _staff(session, name: str) -> Staff:
    person = Staff(full_name=name, normalized_name=name.lower())
    session.add(person)
    await session.flush()
    return person


async def _say(
    session, chat: Chat, n: int, side: BusinessSide, minutes: int, text: str,
    *, label: str, requires: bool | None = None, substantive: bool | None = None,
    by: Staff | None = None,
) -> Message:
    message = Message(
        chat_id=chat.id,
        tg_message_id=n,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=side,
        text=text,
        char_count=len(text),
        sent_at=T0 + timedelta(minutes=minutes),
    )
    session.add(message)
    await session.flush()
    session.add(
        Classification(
            message_id=message.id,
            model=get_settings().ai_model,
            prompt_version=9,
            source="model",
            label=label,
            requires_response=requires,
            is_substantive=substantive,
        )
    )
    if by is not None:
        session.add(Attribution(message_id=message.id, staff_id=by.id))
    await session.flush()
    return message


async def _episode(session, chat: Chat) -> Interaction:
    return await session.scalar(select(Interaction).where(Interaction.chat_id == chat.id))


@requires_db
async def test_specialist_ack_after_handoff_closes_second_layer(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _chat(session, -100777601)
    irina = await _staff(session, "Ирина Соколова")
    evgenia = await _staff(session, "Евгения Фролова")
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "Сколько мы должны Григорьеву?",
               label="question", requires=True)
    await _say(session, chat, 2, BusinessSide.COMPANY, 1, "передала запрос бухгалтеру",
               label="handoff", substantive=False, by=irina)
    reply = await _say(session, chat, 3, BusinessSide.COMPANY, 85,
                       "Добрый день, расчеты посмотрю сегодня и отпишусь",
                       label="ack", substantive=False, by=evgenia)

    # Через двое суток ответа «по существу» так и не было.
    await rebuild_interactions(session, now=T0 + timedelta(days=2))
    await session.flush()

    episode = await _episode(session, chat)
    assert episode.handoff_at is not None
    assert episode.substantive_at == reply.sent_at, "выход специалиста на связь закрывает слой"
    assert episode.substantive_staff_id == evgenia.id
    assert episode.substantive_breached is False, "ответил через 85 минут — срок в сутки не нарушен"
    assert episode.state is InteractionState.ANSWERED


@requires_db
async def test_specialist_counter_question_satisfies_layer_but_keeps_case_open(session, monkeypatch):
    """«Уточните, какие справки?» от специалиста — вышел на связь, но ход за клиентом.

    Если встречный вопрос закрыл бы обращение, ответ клиента «Да, 2-НДФЛ…»
    открыл бы новое и через полчаса дал ложный 🔴.
    """
    from app.services.alerts import KIND_NO_SUBSTANTIVE  # noqa: F401 — смысловая ссылка

    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _chat(session, -100777604)
    irina = await _staff(session, "Ирина Соколова")
    tamara = await _staff(session, "Тамара Гришина")
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "Мне нужны справки о доходах",
               label="request", requires=True)
    await _say(session, chat, 2, BusinessSide.COMPANY, 1, "передала запрос бухгалтеру",
               label="handoff", substantive=False, by=irina)
    question = await _say(session, chat, 3, BusinessSide.COMPANY, 15, "Уточните, какие справки нужны?",
                          label="question", substantive=False, by=tamara)
    # Модель ошиблась и сочла ответ клиента новой просьбой.
    await _say(session, chat, 4, BusinessSide.CLIENT, 118, "Да, 2-НДФЛ, справки о доходах",
               label="request", requires=True)

    await rebuild_interactions(session, now=T0 + timedelta(hours=4))
    await session.flush()

    episodes = list((await session.scalars(
        select(Interaction).where(Interaction.chat_id == chat.id).order_by(Interaction.opened_at)
    )).all())
    assert len(episodes) == 1, "ответ клиента на вопрос специалиста открыл второе обращение"
    episode = episodes[0]
    assert episode.substantive_at == question.sent_at, "срок специалиста выполнен вопросом"
    assert episode.client_messages == 2, "ответ клиента присоединился, а не открыл новое"
    # Финальный проход: специалист на связи, ход за клиентом — компания своё
    # сделала, обращение «отвечено» (как после встречного вопроса помощника),
    # а не «ждёт» и не «осталось без ответа». Алерт 🟠 по нему не родится.
    assert episode.state is InteractionState.ANSWERED
    assert episode.substantive_breached is False

    # Документы пришли — теперь закрыто; время до ответа осталось от первого контакта.
    await _say(session, chat, 5, BusinessSide.COMPANY, 300, "Справки во вложении",
               label="substantive", substantive=True, by=tamara)
    await rebuild_interactions(session, now=T0 + timedelta(hours=6))
    await session.flush()
    episode = await _episode(session, chat)
    assert episode.state is InteractionState.ANSWERED
    assert episode.substantive_at == question.sent_at


@requires_db
async def test_assistants_own_ack_after_handoff_does_not_close(session, monkeypatch):
    """«Принято» от самого помощника после его же передачи — специалист ещё молчит."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _chat(session, -100777602)
    irina = await _staff(session, "Ирина Соколова")
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "Как оплатить?", label="question", requires=True)
    await _say(session, chat, 2, BusinessSide.COMPANY, 1, "передала запрос бухгалтеру",
               label="handoff", substantive=False, by=irina)
    await _say(session, chat, 3, BusinessSide.CLIENT, 30, "И ещё справку", label="request", requires=True)
    await _say(session, chat, 4, BusinessSide.COMPANY, 31, "принято", label="ack", substantive=False, by=irina)

    await rebuild_interactions(session, now=T0 + timedelta(hours=3))
    await session.flush()

    episode = await _episode(session, chat)
    assert episode.handoff_at is not None
    assert episode.substantive_at is None, "помощник — не специалист, слой ждёт"
    assert episode.state is InteractionState.REACTED


@requires_db
async def test_unattributed_reply_after_handoff_still_needs_substance(session, monkeypatch):
    """Автор не опознан — кто это, неизвестно; закрывает только ответ по существу."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _chat(session, -100777603)
    irina = await _staff(session, "Ирина Соколова")
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "Как оплатить?", label="question", requires=True)
    await _say(session, chat, 2, BusinessSide.COMPANY, 1, "передала запрос бухгалтеру",
               label="handoff", substantive=False, by=irina)
    await _say(session, chat, 3, BusinessSide.COMPANY, 40, "принято", label="ack", substantive=False)

    await rebuild_interactions(session, now=T0 + timedelta(hours=3))
    await session.flush()

    episode = await _episode(session, chat)
    assert episode.substantive_at is None
