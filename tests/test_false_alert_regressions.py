"""Регрессии ложных алертов.

1. Реплай клиента в обычной (не форум) группе несёт message_thread_id — это
   не отдельная тема, и ответ компании без реплая к нему привязывается.

2. «Спасибо» без ответа компании не склеивается со следующим файлом в одно
   обращение и не отдаёт ему свой срок.
"""

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
from tests.conftest import requires_db

# Даты тестов — среда и четверг: рабочие дни при календаре по умолчанию.
YESTERDAY_1322_MSK = datetime(2026, 8, 26, 10, 22, tzinfo=timezone.utc)
TODAY_1239_MSK = datetime(2026, 8, 27, 9, 39, tzinfo=timezone.utc)
TODAY_1242_MSK = datetime(2026, 8, 27, 9, 42, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 27, 9, 45, tzinfo=timezone.utc)


async def _chat(session, tg_chat_id: int, *, is_forum: bool) -> Chat:
    chat = Chat(
        tg_chat_id=tg_chat_id, title="Сигма", state=ChatState.TRACKED, is_forum=is_forum
    )
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=YESTERDAY_1322_MSK - timedelta(days=2))
    return chat


async def _message(
    session,
    chat,
    tg_message_id: int,
    side: BusinessSide,
    sent_at: datetime,
    *,
    text: str | None = None,
    thread_id: int | None = None,
    media_kind: str | None = None,
) -> Message:
    message = Message(
        chat_id=chat.id,
        tg_message_id=tg_message_id,
        thread_id=thread_id,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=side,
        text=text,
        char_count=len(text or ""),
        has_media=media_kind is not None,
        media_kind=media_kind,
        sent_at=sent_at,
    )
    session.add(message)
    await session.flush()
    return message


@requires_db
async def test_reply_thread_in_regular_group_does_not_break_reaction(session):
    """Реплай клиента в обычной группе — не отдельная тема."""
    chat = await _chat(session, -100999030, is_forum=False)
    opened_at = datetime(2026, 8, 26, 6, 39, tzinfo=timezone.utc)  # 09:39 МСК
    await _message(
        session, chat, 1, BusinessSide.CLIENT, opened_at,
        text="Они по ЭДО исправленный счёт прислали", thread_id=2243,
    )
    await _message(
        session, chat, 2, BusinessSide.COMPANY,
        opened_at + timedelta(minutes=13), text="Доброе утро, принято",
    )

    await rebuild_interactions(session, now=opened_at + timedelta(hours=1))
    await session.flush()

    episode = await session.scalar(
        select(Interaction).where(Interaction.chat_id == chat.id)
    )
    assert episode.first_reaction_at is not None, (
        "реплай клиента оторвал ответ компании — так и рождается "
        "ложный алерт на уже отвеченном обращении"
    )
    assert episode.state is InteractionState.ANSWERED
    assert episode.sla_breached is False


@requires_db
async def test_forum_topics_are_still_respected(session):
    """В настоящем форуме тема — граница: ответ в другой теме не реакция."""
    chat = await _chat(session, -100999031, is_forum=True)
    opened_at = datetime(2026, 8, 26, 6, 39, tzinfo=timezone.utc)
    await _message(
        session, chat, 1, BusinessSide.CLIENT, opened_at,
        text="Вопрос в теме", thread_id=2243,
    )
    await _message(
        session, chat, 2, BusinessSide.COMPANY,
        opened_at + timedelta(minutes=13), text="Ответ в другой теме",
    )

    await rebuild_interactions(session, now=opened_at + timedelta(hours=1))
    await session.flush()

    episode = await session.scalar(
        select(Interaction).where(Interaction.chat_id == chat.id)
    )
    assert episode.first_reaction_at is None
    assert episode.state is InteractionState.OPEN


@requires_db
async def test_handoff_without_question_does_not_open_second_layer(session, monkeypatch):
    """«Передала информацию» после уведомления клиента — не долг специалиста.

    Клиент уведомил («Отправим представителя», ответа не требует), помощник
    переслал внутрь, коллега подтвердила — второй слой не открывается.
    Следующая просьба клиента («набери меня») становится НОВЫМ обращением
    первого слоя, а не хвостом вчерашней пересылки.
    """
    # Вердикты модели должны влиять: без пина тест зависел бы от того, задан
    # ли AI_SHADOW_MODE в окружении (умолчание true — вердикты не учитываются).
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    model = get_settings().ai_model
    chat = await _chat(session, -100999033, is_forum=False)

    notice = await _message(
        session, chat, 1, BusinessSide.CLIENT,
        YESTERDAY_1322_MSK, text="Отправим представителя",
    )
    relay = await _message(
        session, chat, 2, BusinessSide.COMPANY,
        YESTERDAY_1322_MSK + timedelta(seconds=30), text="Передала информацию",
    )
    ack = await _message(
        session, chat, 3, BusinessSide.COMPANY,
        YESTERDAY_1322_MSK + timedelta(minutes=1), text="Поняла, подтверждаю",
    )
    call_me = await _message(
        session, chat, 4, BusinessSide.CLIENT, TODAY_1239_MSK, text="Набери меня, пожалуйста"
    )
    session.add_all(
        [
            Classification(
                message_id=notice.id, model=model, prompt_version=2,
                source="model", label="info", requires_response=False,
            ),
            Classification(
                message_id=relay.id, model=model, prompt_version=2,
                source="model", label="handoff", is_substantive=False,
            ),
            Classification(
                message_id=ack.id, model=model, prompt_version=2,
                source="model", label="ack", is_substantive=False,
            ),
            Classification(
                message_id=call_me.id, model=model, prompt_version=2,
                source="model", label="request", requires_response=True,
            ),
        ]
    )
    await session.flush()

    await rebuild_interactions(session, now=NOW)
    await session.flush()

    episodes = (
        await session.scalars(
            select(Interaction)
            .where(Interaction.chat_id == chat.id)
            .order_by(Interaction.opened_at)
        )
    ).all()
    assert len(episodes) == 2, "просьба позвонить прилипла к вчерашней пересылке"
    yesterday, today = episodes
    assert yesterday.handoff_at is None, (
        "второй слой открылся без вопроса клиента — снова будет алерт "
        "«нет ответа специалиста» про долг, которого нет"
    )
    assert yesterday.state is InteractionState.NO_RESPONSE_NEEDED, (
        "информационная серия должна закрываться как «ответ не требовался», "
        "а не пугать строкой «осталось без ответа»"
    )
    assert today.opened_by_message_id == call_me.id
    assert today.state is InteractionState.OPEN, (
        "просьба клиента должна ждать реакции первым слоем"
    )


@requires_db
async def test_thanks_yesterday_does_not_lend_its_deadline_to_today(session):
    """«Спасибо» вчера + файл сегодня = ДВА обращения, срок — от файла."""
    chat = await _chat(session, -100999032, is_forum=False)
    thanks = await _message(
        session, chat, 1, BusinessSide.CLIENT, YESTERDAY_1322_MSK, text="Спасибо"
    )
    session.add(
        Classification(
            message_id=thanks.id,
            model=get_settings().ai_model,
            prompt_version=2,
            source="rule",
            label="ack",
            requires_response=False,
        )
    )
    document = await _message(
        session, chat, 2, BusinessSide.CLIENT, TODAY_1239_MSK, media_kind="document"
    )
    await _message(
        session, chat, 3, BusinessSide.COMPANY, TODAY_1242_MSK, text="Принято"
    )
    await session.flush()

    await rebuild_interactions(session, now=NOW)
    await session.flush()

    episodes = (
        await session.scalars(
            select(Interaction)
            .where(Interaction.chat_id == chat.id)
            .order_by(Interaction.opened_at)
        )
    ).all()
    assert len(episodes) == 2, (
        "вчерашнее «спасибо» склеилось с сегодняшним вопросом — так и рождается "
        "алерт «просрочено на 22 часа»"
    )
    first, second = episodes
    assert first.opened_by_message_id == thanks.id
    assert first.state is InteractionState.NO_RESPONSE_NEEDED
    assert second.opened_by_message_id == document.id
    assert second.state is InteractionState.ANSWERED
    assert second.sla_breached is False, "срок унаследован от вчерашней вежливости"


@requires_db
async def test_informational_series_is_not_left_without_answer(session, monkeypatch):
    """Разрез по пределу не имеет права переименовывать закрытый разговор.

    Клиент прислал два уведомления (info, ответа не требуют), помощник
    ответил «передала информацию». На следующий день клиент пишет снова,
    и прежнее обращение разрезается по пределу времени — но не получает
    «осталось без ответа»: отвечать было не на что, и реакция была.
    """
    chat = await _chat(session, -100999040, is_forum=False)
    opened_at = datetime(2026, 8, 26, 7, 1, tzinfo=timezone.utc)

    first = await _message(
        session, chat, 1, BusinessSide.CLIENT, opened_at,
        text="В базе маркировку не используем, нам ничего не настраивали",
    )
    second = await _message(
        session, chat, 2, BusinessSide.CLIENT, opened_at + timedelta(minutes=1),
        text="Во второй базе есть настройки по ЕГАИС, в ней установлен модуль",
    )
    reply = await _message(
        session, chat, 3, BusinessSide.COMPANY, opened_at + timedelta(minutes=2),
        text="Доброе утро, передала информацию Вере",
    )
    # Следующий день: клиент снова пишет уведомление. Оно тоже info, поэтому
    # «спасибо»-разрез не срабатывает и дело доходит до предела времени.
    later = await _message(
        session, chat, 4, BusinessSide.CLIENT,
        opened_at + timedelta(days=1, hours=4), text="Оплату по счёту провели, всё в порядке",
    )

    model = get_settings().ai_model
    # Сообщения клиента — информирование: ответа не требуют.
    for message in (first, second, later):
        session.add(
            Classification(
                message_id=message.id,
                model=model,
                prompt_version=2,
                requires_response=False,
                label="info",
                source="model",
            )
        )
    # Ответ помощника модель считает передачей — значит содержательным
    # ответом он не засчитывается, и обращение остаётся «ждущим».
    session.add(
        Classification(
            message_id=reply.id,
            model=model,
            prompt_version=2,
            is_substantive=False,
            label="handoff",
            source="model",
        )
    )
    await session.flush()

    # Тест опирается на вердикты МОДЕЛИ — тихий режим обязан быть выключен,
    # иначе в CI (где умолчание true) вердикты не учитываются вовсе.
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)

    await rebuild_interactions(session, now=opened_at + timedelta(days=1, hours=5))
    await session.flush()

    episodes = (
        await session.scalars(
            select(Interaction).where(Interaction.chat_id == chat.id).order_by(Interaction.opened_at)
        )
    ).all()

    # Два обращения — значит разрез по пределу действительно случился
    # и проверяется именно его ветка.
    assert len(episodes) == 2, "разрез не сработал — тест проверяет не то"
    assert episodes[0].state is InteractionState.NO_RESPONSE_NEEDED, (
        f"информационная серия закрыта как {episodes[0].state.value}, "
        "а владельцу это показывается как «осталось без ответа»"
    )
    # Реакция на информационную серию НЕ записывается: клиенту было
    # не на что отвечать, и реплика компании не даёт ни ttfr, ни просрочки.
    assert episodes[0].first_reaction_at is None, "реакция на серию без вопроса не считается"
    assert episodes[0].sla_breached is None
