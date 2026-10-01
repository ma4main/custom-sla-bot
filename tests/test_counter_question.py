"""Встречный вопрос компании (метка `question`) — не ответ по существу:
обращение остаётся открытым, и ответ клиента присоединяется к нему, а не
открывает новое обращение со своим таймером."""

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
from app.services.episodes import rebuild_interactions
from app.services.tracking import open_period
from app.services.verdicts import SOURCE_MODEL
from tests.conftest import requires_db

MSK = timezone(timedelta(hours=3))

ASKED = datetime(2026, 9, 2, 15, 52, tzinfo=MSK)
ACK = datetime(2026, 9, 2, 15, 53, tzinfo=MSK)
COUNTER = datetime(2026, 9, 2, 17, 16, tzinfo=MSK)
ANSWER = datetime(2026, 9, 2, 17, 20, tzinfo=MSK)
ALERT_TIME = datetime(2026, 9, 2, 17, 50, tzinfo=MSK)


async def _message(session, chat, tg_id, side, sent_at, text, *, label, **verdict):
    message = Message(
        chat_id=chat.id,
        tg_message_id=tg_id,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=side,
        text=text,
        char_count=len(text),
        sent_at=sent_at,
    )
    session.add(message)
    await session.flush()
    session.add(
        Classification(
            message_id=message.id,
            model=get_settings().ai_model,
            prompt_version=3,
            source=SOURCE_MODEL,
            label=label,
            **verdict,
        )
    )
    await session.flush()
    return message


async def _tekhnostroy(session, *, counter_label: str) -> Chat:
    """Переписка кейса; метка встречного вопроса — параметр теста."""
    chat = Chat(tg_chat_id=-100910001, title="ВЕКТОР", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=ASKED - timedelta(days=1))

    await _message(
        session, chat, 521, BusinessSide.CLIENT, ASKED,
        "Добрый день. Сделайте пожалуйста счёт на аванс 150т",
        label="request", requires_response=True,
    )
    await _message(
        session, chat, 522, BusinessSide.COMPANY, ACK, "Добрый день, принято",
        label="ack", is_substantive=False,
    )
    await _message(
        session, chat, 523, BusinessSide.COMPANY, COUNTER,
        "Уточните, запрос был от ИП, от Вектор тоже нужен или ошибочно направлен?",
        label=counter_label, is_substantive=(counter_label == "substantive"),
    )
    await _message(
        session, chat, 524, BusinessSide.CLIENT, ANSWER,
        "Ошибся, по трём договорам на ИП",
        label="info", requires_response=True,
    )
    return chat


async def _episodes(session, chat_id: int) -> list[Interaction]:
    return list(
        await session.scalars(
            select(Interaction)
            .where(Interaction.chat_id == chat_id)
            .order_by(Interaction.opened_at)
        )
    )


@requires_db
async def test_counter_question_does_not_close_the_case(session, monkeypatch):
    """Встречный вопрос не закрывает обращение и не плодит второе."""
    # Вердикты модели учитываются только вне тихого режима, а в CI по умолчанию
    # режим тихий — поэтому он выключается явно.
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False)
    chat = await _tekhnostroy(session, counter_label="question")
    await rebuild_interactions(session, now=ALERT_TIME)
    await session.flush()

    episodes = await _episodes(session, chat.id)
    assert len(episodes) == 1, (
        "ответ клиента на наш вопрос открыл новое обращение — по нему и летел алерт"
    )
    episode = episodes[0]
    assert episode.opened_at == ASKED, "обращение — то самое, с просьбы клиента"
    assert episode.client_messages == 2, "ответ клиента присоединился к обращению"
    assert episode.substantive_at is None, (
        "встречный вопрос засчитан ответом по существу — счёт-то не выставлен"
    )
    assert episode.first_reaction_at == ACK, "«принято» — по-прежнему реакция"
    assert episode.sla_breached is False, "реакция за минуту — никакой просрочки"
    # Реакция была, передачи не было — помощник ведёт вопрос сам, ждать
    # больше некого. Обращение
    # закрывается «отвечено», но БЕЗ времени ответа по существу: метрика
    # скорости на нём не построится, а алерт по нему не уйдёт.
    assert episode.state is InteractionState.ANSWERED
    assert episode.ttfa_business_seconds is None


@requires_db
async def test_old_behaviour_is_what_owner_dismissed(session, monkeypatch):
    """Контроль: с меткой `substantive` встречный вопрос закрывает обращение,
    а ответ клиента открывает второе — дело именно в метке."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False)
    chat = await _tekhnostroy(session, counter_label="substantive")
    await rebuild_interactions(session, now=ALERT_TIME)
    await session.flush()

    episodes = await _episodes(session, chat.id)
    assert len(episodes) == 2, "именно так и появилось второе обращение"
    assert episodes[0].substantive_at == COUNTER
    assert episodes[1].opened_at == ANSWER
    assert episodes[1].first_reaction_at is None, (
        "второе обращение ждёт реакции — через 30 минут по нему уходит алерт"
    )


@requires_db
async def test_waiting_for_the_client_is_not_blamed_on_the_company(session):
    """Клиент так и не ответил — компанию в «остались без ответа» не пишем.

    Встречный вопрос оставляет обращение открытым, и когда окно ожидания
    истечёт, сработает существующее правило «реакция была, передачи не
    было — помощник закрыл вопрос сам». Провалом компании это не считается:
    ход был за клиентом.
    """
    chat = Chat(tg_chat_id=-100910002, title="Молчит клиент", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=ASKED - timedelta(days=1))
    await _message(
        session, chat, 1, BusinessSide.CLIENT, ASKED, "Сделайте счёт на аванс",
        label="request", requires_response=True,
    )
    await _message(
        session, chat, 2, BusinessSide.COMPANY, COUNTER, "Уточните, счёт на ИП или на ООО?",
        label="question", is_substantive=False,
    )

    # Двое суток спустя: окно ожидания (сутки после срока) давно истекло.
    await rebuild_interactions(session, now=ASKED + timedelta(days=2))
    await session.flush()

    episode = (await _episodes(session, chat.id))[0]
    assert episode.state is not InteractionState.ABANDONED, (
        "ждали клиента, а «остались без ответа» записали на компанию"
    )
    assert episode.first_reaction_at == COUNTER, "встречный вопрос — это реакция"


def test_model_verdict_accepts_the_new_label():
    from app.services.ai import validate_verdict

    verdict = validate_verdict({"label": "question"}, is_client=False)
    assert verdict == {
        "label": "question",
        "is_substantive": False,
        # Связь с открытым обращением у вердикта есть всегда;
        # модель её не назвала — значит «неизвестна», а не «нет поля».
        "answers_request_id": None,
    }


def test_old_verdicts_are_not_requeued_after_a_prompt_change():
    """Новые правила — только для новых сообщений.

    Условие очереди не должно смотреть на версию промпта, иначе смена
    правил отправит на переспрос всю историю и перепишет метрики закрытых
    периодов.
    """
    import inspect

    from app.services import ai_stats

    source = inspect.getsource(ai_stats.pending_conditions)
    assert "prompt_version ==" not in source.replace("`prompt_version == PROMPT_VERSION`", ""), (
        "очередь снова отбирает по версии промпта — история поедет при смене правил"
    )
