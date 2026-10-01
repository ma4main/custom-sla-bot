"""Правила движка v2 и дата перехода на них.

Реальный `rebuild_interactions` на изолированной базе; метки и авторы заданы
явно. Каждое правило проверяется парой «до границы `EPISODE_RULES_V2_SINCE` —
v1, после — v2»."""

from datetime import datetime, timedelta, timezone

import pytest
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

CLIENT, COMPANY = BusinessSide.CLIENT, BusinessSide.COMPANY
BOT = TransportActorKind.INTEGRATOR_BOT

# Понедельник, 10:30 МСК — рабочее время при календаре по умолчанию.
T0 = datetime(2026, 9, 7, 7, 30, tzinfo=timezone.utc)
V2_ON = T0 - timedelta(days=1)
NOTICE = "Вы не авторизованы! Воспользуйтесь командой /auth"
VACATION = (
    "Нина Тестова [corp.example] пишет:\n\nДобрый день. Уведомляю о том, что с 10.08 по 23.08 "
    "буду находиться в отпуске. Если что-то срочное, заменяет меня Ксения."
)


@pytest.fixture
def rules(monkeypatch, request):
    """Версия правил: `2` включает v2 с V2_ON, `1` — v1 везде."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    version = getattr(request, "param", 2)
    monkeypatch.setattr(
        get_settings(), "episode_rules_v2_since", V2_ON if version == 2 else None, raising=False
    )
    return version


async def _chat(session, tg_chat_id: int) -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title="Стенд v2", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=T0 - timedelta(days=2))
    return chat


async def _staff(session, name: str) -> Staff:
    person = Staff(full_name=name, normalized_name=name.lower())
    session.add(person)
    await session.flush()
    return person


async def _say(
    session, chat: Chat, n: int, side: BusinessSide, minutes: float, text: str | None,
    *, label: str | None = None, requires: bool | None = None, substantive: bool | None = None,
    media: str | None = None, actor: TransportActorKind = TransportActorKind.HUMAN_USER,
    staff: Staff | None = None, raw_name: str | None = None,
) -> Message:
    message = Message(
        chat_id=chat.id,
        tg_message_id=n,
        transport_actor_kind=actor,
        business_side=side,
        text=text,
        char_count=len(text or ""),
        media_kind=media,
        has_media=media is not None,
        sent_at=T0 + timedelta(minutes=minutes),
    )
    session.add(message)
    await session.flush()
    if label is not None:
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
    if staff is not None or raw_name is not None:
        session.add(
            Attribution(
                message_id=message.id,
                staff_id=staff.id if staff else None,
                raw_name=raw_name,
                parser_version=2,
            )
        )
    await session.flush()
    return message


async def _episodes(session, chat: Chat) -> list[Interaction]:
    return list(
        (
            await session.scalars(
                select(Interaction).where(Interaction.chat_id == chat.id).order_by(Interaction.opened_at)
            )
        ).all()
    )


async def _rebuild(session, hours: float = 3) -> None:
    await rebuild_interactions(session, now=T0 + timedelta(hours=hours))
    await session.flush()


# ── «Система», служебные уведомления, рассылки ──────────────────────────


@requires_db
@pytest.mark.parametrize("rules", [1, 2], indirect=True)
async def test_system_signature_is_a_reaction_and_in_v2_a_specialist_contact(session, rules):
    """Подпись без сотрудника в справочнике: реакция всегда; после передачи
    в v2 — контакт специалиста, в личную статистику не попадает (staff_id пуст)."""
    chat = await _chat(session, -100778001)
    request = await _say(session, chat, 1, CLIENT, 0, "Скан доверенности не загружен", label="request", requires=True)
    handoff = await _say(
        session, chat, 2, COMPANY, 1, "Система [corp] пишет:\n\nДобрый день, передала запрос бухгалтеру",
        label="handoff", substantive=False, actor=BOT, raw_name="Система",
    )
    contact = await _say(
        session, chat, 3, COMPANY, 40, "Система [corp] пишет:\n\nВижу, приняла в работу",
        label="ack", substantive=False, actor=BOT, raw_name="Система",
    )
    await _rebuild(session)

    (episode,) = await _episodes(session, chat)
    assert episode.opened_by_message_id == request.id
    assert episode.first_reaction_message_id == handoff.id
    assert episode.first_reaction_staff_id is None
    assert episode.sla_breached is False
    assert episode.handoff_at == handoff.sent_at
    if rules == 2:
        assert episode.substantive_message_id == contact.id
        assert episode.substantive_staff_id is None
        assert episode.state is InteractionState.ANSWERED
    else:
        assert episode.substantive_at is None


@requires_db
@pytest.mark.parametrize("rules", [1, 2], indirect=True)
async def test_integrator_notice_is_not_a_reaction_after_cutover(session, rules):
    """«Вы не авторизованы» от бота без подписи: в v1 закрывало первую реакцию,
    в v2 реакция — ответ человека, и просрочка видна."""
    chat = await _chat(session, -100778002)
    staff = await _staff(session, "Вера Тестова")
    await _say(session, chat, 1, CLIENT, 0, "Подпишите акт сверки в ЭДО", label="request", requires=True)
    notice = await _say(session, chat, 2, COMPANY, 5, NOTICE, actor=BOT)
    human = await _say(
        session, chat, 3, COMPANY, 45, "Вера Тестова [corp] пишет:\n\nДокументы подписаны",
        label="substantive", substantive=True, actor=BOT, staff=staff,
    )
    await _rebuild(session)

    (episode,) = await _episodes(session, chat)
    assert episode.version == rules
    if rules == 2:
        assert episode.first_reaction_message_id == human.id
        assert episode.sla_breached is True
    else:
        assert episode.first_reaction_message_id == notice.id
        assert episode.sla_breached is False


@requires_db
@pytest.mark.parametrize("rules", [1, 2], indirect=True)
async def test_broadcast_to_three_chats_is_not_a_reaction(session, rules):
    """Одинаковое тело длиннее 60 символов в трёх чатах за 10 минут — рассылка:
    обращения продолжают ждать человека (v2); в v1 это была реакция."""
    staff = await _staff(session, "Нина Тестова")
    chats = [await _chat(session, -100778010 - i) for i in range(3)]
    for i, chat in enumerate(chats):
        await _say(session, chat, 1, CLIENT, 0, "Сделайте счёт на аванс", label="request", requires=True)
        await _say(session, chat, 2, COMPANY, 5 + i, VACATION, label="ack", substantive=False, actor=BOT, staff=staff)
    await _rebuild(session)

    for chat in chats:
        (episode,) = await _episodes(session, chat)
        if rules == 2:
            assert episode.first_reaction_at is None
            assert episode.state is InteractionState.OPEN
        else:
            assert episode.first_reaction_at is not None


@requires_db
async def test_broadcast_question_still_hands_the_turn_to_the_client(session, rules):
    """Вопрос клиентам в рассылке — не реакция, но ответ клиента не просьба."""
    staff = await _staff(session, "Ксения Тестова")
    question = "Ксения Тестова [corp] пишет:\n\nДобрый день. Подскажите, пожалуйста, когда будут готовы эти пункты?"
    chats = [await _chat(session, -100778020 - i) for i in range(3)]
    for i, chat in enumerate(chats):
        await _say(session, chat, 1, CLIENT, 0, "Нужна выписка", label="request", requires=True)
        await _say(session, chat, 2, COMPANY, 5 + i, question, label="question", substantive=False, actor=BOT, staff=staff)
    reply = await _say(session, chats[0], 3, CLIENT, 12, "На следующей неделе", label="request", requires=True)
    await _rebuild(session)

    (episode,) = await _episodes(session, chats[0])
    assert episode.first_reaction_at is None, "рассылка — не реакция"
    assert episode.client_messages == 2 and episode.last_client_at == reply.sent_at, "ответ клиента — не новое обращение"


# ── Вопрос компании и ответ клиента при закрытом обращении ─────────────


async def _closed_then_question(session, chat: Chat, staff: Staff, *, question_label: str = "question",
                                question_text: str = "На ИП или на ООО нужен счёт?") -> None:
    await _say(session, chat, 1, CLIENT, 0, "Сделайте счёт", label="request", requires=True)
    await _say(session, chat, 2, COMPANY, 2, "Счёт готов", label="substantive", substantive=True, staff=staff)
    await _say(session, chat, 3, COMPANY, 3, question_text, label=question_label,
               substantive=question_label == "substantive", staff=staff)


@requires_db
@pytest.mark.parametrize("rules", [1, 2], indirect=True)
async def test_first_client_reply_to_company_question_gets_no_timer(session, rules):
    """Первая реплика клиента после вопроса компании при закрытом обращении —
    ответ без таймера, даже если модель пометила её просьбой; вторая реплика
    живёт по обычным правилам."""
    chat = await _chat(session, -100778030)
    staff = await _staff(session, "Ирина Тестова")
    await _closed_then_question(session, chat, staff)
    answer = await _say(session, chat, 4, CLIENT, 10, "На ООО, по двум договорам", label="request", requires=True)
    second = await _say(session, chat, 5, CLIENT, 12, "И ещё пришлите акт сверки", label="request", requires=True)
    await _rebuild(session)

    episodes = await _episodes(session, chat)
    by_opener = {episode.opened_by_message_id: episode for episode in episodes}
    if rules == 2:
        assert by_opener[answer.id].state is InteractionState.NO_RESPONSE_NEEDED
        assert by_opener[answer.id].sla_breached is None
        assert by_opener[second.id].state is InteractionState.OPEN, "вторая просьба ждёт своей реакции"
    else:
        assert by_opener[answer.id].state is InteractionState.OPEN, "v1: ответ открывал таймер"


@requires_db
async def test_company_text_ending_with_question_mark_hands_the_turn(session, rules):
    """Промпт помечает «ответ + уточнение» как substantive: знак вопроса в конце
    реплики компании тоже взводит «ждём клиента»."""
    chat = await _chat(session, -100778031)
    staff = await _staff(session, "Ирина Тестова")
    await _closed_then_question(
        session, chat, staff, question_label="substantive",
        question_text="Уточните, запрос был от ИП, от Вектор тоже нужен или ошибочно направлен?",
    )
    answer = await _say(session, chat, 4, CLIENT, 10, "Ошибся, по трём договорам на ИП", label="info", requires=True)
    await _rebuild(session)

    by_opener = {episode.opened_by_message_id: episode for episode in await _episodes(session, chat)}
    assert by_opener[answer.id].state is InteractionState.NO_RESPONSE_NEEDED


@requires_db
async def test_client_turn_expires_same_time_next_workday(session, rules):
    """Ход у клиента до того же времени следующего рабочего дня; позже его
    слово судится само по себе — давний вопрос не глотает новую просьбу."""
    chat = await _chat(session, -100778032)
    staff = await _staff(session, "Ирина Тестова")
    await _closed_then_question(session, chat, staff)  # вопрос в 10:33 понедельника
    late = await _say(session, chat, 4, CLIENT, 2 * 24 * 60, "Сделайте акт сверки", label="request", requires=True)
    await rebuild_interactions(session, now=T0 + timedelta(days=2, hours=3))
    await session.flush()

    by_opener = {episode.opened_by_message_id: episode for episode in await _episodes(session, chat)}
    assert by_opener[late.id].state is InteractionState.OPEN
    assert by_opener[late.id].first_reaction_at is None


# ── Вложения без текста ─────────────────────────────────────────────────


@requires_db
@pytest.mark.parametrize("rules", [1, 2], indirect=True)
async def test_requested_scans_with_thanks_in_between_get_no_timer(session, rules):
    """Запрошенные сканы приходят пачкой; «спасибо» компании и пояснение
    клиента между ними серию не рвут. Файл через 40 минут — новое обращение."""
    chat = await _chat(session, -100778040)
    staff = await _staff(session, "Ирина Тестова")
    await _say(session, chat, 1, CLIENT, 0, "Отправьте акты по ЭДО", label="request", requires=True)
    await _say(session, chat, 2, COMPANY, 2, "готово", label="substantive", substantive=True, staff=staff)
    await _say(session, chat, 3, COMPANY, 5, "а сканы можете прислать?", label="question", substantive=False, staff=staff)
    first = await _say(session, chat, 4, CLIENT, 6, None, media="document")
    await _say(session, chat, 5, CLIENT, 7, None, media="photo")
    thanks = await _say(session, chat, 6, COMPANY, 7.5, "спасибо большое, как всё соберу — отпишусь", label="ack", substantive=False, staff=staff)
    note = await _say(session, chat, 7, CLIENT, 8, "1204 чек утерян", label="info", requires=False)
    third = await _say(session, chat, 8, CLIENT, 9, None, media="document")
    later = await _say(session, chat, 9, CLIENT, 49, None, media="document")
    await _rebuild(session)

    episodes = await _episodes(session, chat)
    by_opener = {episode.opened_by_message_id: episode for episode in episodes}
    open_openers = {episode.opened_by_message_id for episode in episodes if episode.state is InteractionState.OPEN}
    if rules == 2:
        # Ответ клиента: первые два скана — обращение «ответа не требовалось».
        assert by_opener[first.id].state is InteractionState.NO_RESPONSE_NEEDED
        assert by_opener[first.id].client_messages == 2
        # Пояснение и третий скан после «спасибо» компании — та же серия, без таймера.
        assert by_opener[note.id].state is InteractionState.NO_RESPONSE_NEEDED
        assert third.id not in by_opener
        # Файл спустя 40 минут — новое обращение.
        assert open_openers == {later.id}
    else:
        # v1: вопрос компании при закрытом обращении не учитывался, скан без
        # вердикта открывал обращение, «спасибо» стало его реакцией, а всё
        # последующее приклеилось к нему — таймера нет, но нет и ответа.
        assert by_opener[first.id].first_reaction_message_id == thanks.id
        assert by_opener[first.id].client_messages == 5
        assert open_openers == set()


@requires_db
async def test_second_unprompted_attachment_after_acknowledgement_opens_a_timer(session, rules):
    """Второе фото через минуту после первого, на которое компания уже
    ответила «чек передан», — новое обращение. Правила «та же пачка» нет:
    без содержимого его не отличить, перекос остаётся в пользу клиента."""
    chat = await _chat(session, -100778041)
    staff = await _staff(session, "Ирина Тестова")
    first = await _say(session, chat, 1, CLIENT, 0, None, media="photo")
    await _say(session, chat, 2, COMPANY, 3, "чек передан", label="substantive", substantive=True, staff=staff)
    second = await _say(session, chat, 3, CLIENT, 4, None, media="photo")
    await _rebuild(session)

    by_opener = {episode.opened_by_message_id: episode for episode in await _episodes(session, chat)}
    assert by_opener[first.id].state is InteractionState.ANSWERED
    assert by_opener[second.id].state is InteractionState.OPEN
    assert by_opener[second.id].first_reaction_at is None


@requires_db
async def test_company_screenshot_after_question_keeps_the_client_turn(session, rules):
    """Вопрос компании и следом скриншот без текста: ход остаётся у клиента,
    его ответ «Все не то» — не просьба."""
    chat = await _chat(session, -100778043)
    staff = await _staff(session, "Ирина Тестова")
    await _say(session, chat, 1, CLIENT, 0, "Приходили ли приглашения в Диадок?", label="question", requires=True)
    await _say(session, chat, 2, COMPANY, 5, "уточню", label="ack", substantive=False, staff=staff)
    await _say(
        session, chat, 3, COMPANY, 18, "сейчас 2 приглашения, уточните какое принять ?",
        label="substantive", substantive=True, staff=staff,
    )
    await _say(session, chat, 4, COMPANY, 18.2, None, label="substantive", substantive=True, media="document", staff=staff)
    reply = await _say(session, chat, 5, CLIENT, 23, "Все не то", label="info", requires=True)
    await _rebuild(session)

    by_opener = {episode.opened_by_message_id: episode for episode in await _episodes(session, chat)}
    assert by_opener[reply.id].state is InteractionState.NO_RESPONSE_NEEDED


@requires_db
async def test_unprompted_attachment_still_opens_a_timer(session, rules):
    """Непрошеный файл при закрытом обращении — новое обращение с 30 минутами."""
    chat = await _chat(session, -100778042)
    staff = await _staff(session, "Ирина Тестова")
    await _say(session, chat, 1, CLIENT, 0, "Сделайте счёт", label="request", requires=True)
    await _say(session, chat, 2, COMPANY, 2, "Счёт готов", label="substantive", substantive=True, staff=staff)
    document = await _say(session, chat, 3, CLIENT, 60, None, media="document")
    await _rebuild(session)

    by_opener = {episode.opened_by_message_id: episode for episode in await _episodes(session, chat)}
    assert by_opener[document.id].state is InteractionState.OPEN
    assert by_opener[document.id].first_reaction_at is None


# ── Граница версий ──────────────────────────────────────────────────────


@requires_db
async def test_cutover_keeps_earlier_interactions_on_v1(session, monkeypatch):
    """Обращение, открытое до границы, целиком считается по v1 — даже ответы
    после границы; открытое после — по v2. Границу не сдвигают."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    monkeypatch.setattr(get_settings(), "episode_rules_v2_since", T0 + timedelta(minutes=30), raising=False)
    chat = await _chat(session, -100778050)
    staff = await _staff(session, "Вера Тестова")
    before = await _say(session, chat, 1, CLIENT, 0, "Подпишите акт", label="request", requires=True)
    notice = await _say(session, chat, 2, COMPANY, 40, NOTICE, actor=BOT)
    await _say(session, chat, 3, COMPANY, 45, "Подписано", label="substantive", substantive=True, staff=staff)
    after = await _say(session, chat, 4, CLIENT, 60, "И счёт сделайте", label="request", requires=True)
    await _say(session, chat, 5, COMPANY, 65, NOTICE, actor=BOT)
    await _rebuild(session)

    by_opener = {episode.opened_by_message_id: episode for episode in await _episodes(session, chat)}
    assert by_opener[before.id].version == 1
    assert by_opener[before.id].first_reaction_message_id == notice.id
    assert by_opener[after.id].version == 2
    assert by_opener[after.id].first_reaction_at is None
