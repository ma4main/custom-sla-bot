"""Разметка «кто есть кто в чате»: решение человека о стороне отправителя
перекрывает код, действует задним числом и переживает пересчёт; очередь,
«Участники», подсказка по «/auth» и право `attribution.assign`."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.bot.callbacks import SenderAction
from app.config import get_settings
from app.db.models import (
    Attribution,
    AttributionMethod,
    AuditLog,
    BotRole,
    BotUser,
    BotUserState,
    BusinessSide,
    Chat,
    ChatState,
    Classification,
    Message,
    SenderRule,
    SenderRuleKind,
    SenderRuleSide,
    Staff,
    TransportActorKind,
)
from app.services.access import AccessError
from app.services.ingestion import ANONYMOUS_ADMIN_BOT_ID, resolve_business_side
from app.services.sender_rules import (
    auth_hint,
    clear_rule,
    load_rule,
    parse_auth_name,
    set_rule,
    unruled_bot_senders,
    window_participants,
)
from app.services.staff import normalize_name
from tests.conftest import requires_db

BASE = datetime(2026, 9, 22, 9, tzinfo=timezone.utc)

PERSON_TG = 880101  # сотрудник, пишущий из Telegram напрямую
CLIENT_TG = 880102
BOT_TG = 6543210987  # чужой бот-рассылка
OWNER_TG = 880900


def _owner(role: BotRole = BotRole.OWNER, tg_user_id: int = OWNER_TG) -> BotUser:
    return BotUser(
        tg_user_id=tg_user_id, role=role, permissions={}, state=BotUserState.ACTIVE
    )


async def _chat(session, tg_chat_id: int, title: str = "Бухгалтерия") -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title=title, state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    return chat


async def _say(
    session,
    chat: Chat,
    tg_id: int,
    tg_user_id: int,
    *,
    side: BusinessSide,
    actor: TransportActorKind = TransportActorKind.HUMAN_USER,
    text: str = "текст",
    minutes: int = 0,
) -> Message:
    message = Message(
        chat_id=chat.id,
        tg_message_id=tg_id,
        tg_user_id=tg_user_id,
        transport_actor_kind=actor,
        business_side=side,
        text=text,
        char_count=len(text),
        sent_at=BASE + timedelta(minutes=minutes),
    )
    session.add(message)
    await session.flush()
    return message


async def _staff(session, full_name: str) -> Staff:
    person = Staff(full_name=full_name, normalized_name=normalize_name(full_name))
    session.add(person)
    await session.flush()
    return person


# ── Чистые проверки, без базы ────────────────────────────────────────────


def test_sender_buttons_fit_the_telegram_limit():
    """64 байта на callback data — предел Telegram, а не рекомендация.

    Худший случай: 13-значный ID бота, чат анонимного админа, выбранный
    человек и обе границы показанного окна выписки в одной кнопке.
    """
    samples = [
        SenderAction(action="card", key=ANONYMOUS_ADMIN_BOT_ID),
        SenderAction(action="parts", msg_id=123456789, to_id=123456999),
        SenderAction(
            action="bind",
            key=6543210987654,
            chat_id=999999,
            staff_id=424242,
            msg_id=123456789,
            to_id=123456999,
        ),
        SenderAction(
            action="auth",
            key=ANONYMOUS_ADMIN_BOT_ID,
            chat_id=999999,
            staff_id=424242,
            msg_id=123456789,
            to_id=123456999,
        ),
        SenderAction(
            action="roles", key=6543210987654, chat_id=999999,
            msg_id=123456789, to_id=123456999,
        ),
    ]
    for sample in samples:
        packed = sample.pack()
        assert len(packed.encode()) <= 64, f"больше 64 байт: {packed}"
        assert SenderAction.unpack(packed) == sample


def test_portal_reply_names_the_person():
    """«Вы авторизованы на … как Имя Фамилия» — имя читается, мусор нет."""
    assert (
        parse_auth_name(
            "@GroupAnonymousBot Вы авторизованы на corp.example.com "
            "как Вера Павлова"
        )
        == "Вера Павлова"
    )
    assert (
        parse_auth_name("Вы авторизованы на corp.example.com как Ирина Соколова.")
        == "Ирина Соколова"
    )
    assert parse_auth_name("Вы не авторизованы. Наберите /auth") is None
    assert parse_auth_name("Добрый день, отправила акт") is None
    assert parse_auth_name(None) is None


# ── Правило перекрывает код ──────────────────────────────────────────────


@requires_db
async def test_rule_overrides_code_for_a_person(session):
    """Человек: код зовёт его клиентом, решение человека — сильнее.

    Проверяются все три стороны: именно так чинится «клиент, которого код
    принял за сотрудника» и «сотрудник, пишущий из Telegram напрямую».
    """
    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -1009220001)
    person = await _staff(session, "Ирина Соколова")

    async def side() -> BusinessSide:
        return await resolve_business_side(
            session, TransportActorKind.HUMAN_USER, PERSON_TG, "текст", chat_id=chat.id
        )

    assert await side() is BusinessSide.CLIENT, "код и правда зовёт участника клиентом"

    for rule_side, expected in (
        (SenderRuleSide.COMPANY, BusinessSide.COMPANY),
        (SenderRuleSide.CLIENT, BusinessSide.CLIENT),
        (SenderRuleSide.SYSTEM, BusinessSide.INTEGRATOR_SYSTEM),
    ):
        await set_rule(
            session,
            owner,
            kind=SenderRuleKind.TG_USER,
            key=PERSON_TG,
            chat_id=None,
            side=rule_side,
            staff_id=person.id if rule_side is SenderRuleSide.COMPANY else None,
        )
        assert await side() is expected, f"правило {rule_side.value} не подействовало"

    # Правило одно на отправителя: перевыбор стороны не плодит строки.
    rules = (
        await session.scalars(select(SenderRule).where(SenderRule.key == PERSON_TG))
    ).all()
    assert len(rules) == 1
    assert rules[0].staff_id is None, "у системы автора-сотрудника быть не может"


@requires_db
async def test_rule_for_a_bot_works_in_every_chat(session):
    """Чужой бот узнаётся по Telegram ID — решение действует во всех чатах."""
    owner = _owner()
    session.add(owner)
    first = await _chat(session, -1009220002)
    second = await _chat(session, -1009220003, "Второй чат")

    async def side(chat: Chat) -> BusinessSide:
        return await resolve_business_side(
            session, TransportActorKind.OTHER_BOT, BOT_TG, "Заявка принята", chat_id=chat.id
        )

    assert await side(first) is BusinessSide.UNKNOWN, "без решения чужой бот ничей"

    await set_rule(
        session,
        owner,
        kind=SenderRuleKind.BOT,
        key=BOT_TG,
        chat_id=None,
        side=SenderRuleSide.SYSTEM,
    )
    assert await side(first) is BusinessSide.INTEGRATOR_SYSTEM
    assert await side(second) is BusinessSide.INTEGRATOR_SYSTEM


@requires_db
async def test_anonymous_admin_rule_lives_in_one_chat(session):
    """«От имени группы» — решение на ЧАТ: в другом за учёткой другой человек.

    Учётка GroupAnonymousBot одна на весь Telegram. Решение «это Лидия»,
    разошедшееся по всем чатам, приписало бы ей чужую переписку.
    """
    owner = _owner()
    session.add(owner)
    mine = await _chat(session, -1009220004)
    other = await _chat(session, -1009220005, "Чужой чат")
    person = await _staff(session, "Лидия Кузнецова")

    async def side(chat: Chat) -> BusinessSide:
        return await resolve_business_side(
            session,
            TransportActorKind.OTHER_BOT,
            ANONYMOUS_ADMIN_BOT_ID,
            "Проведено",
            chat_id=chat.id,
        )

    assert await side(mine) is BusinessSide.COMPANY, "по коду анонимный админ — компания"

    await set_rule(
        session,
        owner,
        kind=SenderRuleKind.ANONYMOUS_ADMIN,
        key=ANONYMOUS_ADMIN_BOT_ID,
        chat_id=mine.id,
        side=SenderRuleSide.CLIENT,
    )
    assert await side(mine) is BusinessSide.CLIENT
    assert await side(other) is BusinessSide.COMPANY, "решение уехало в соседний чат"

    # Правило «на все чаты» для этой учётки невозможно даже мимо экрана.
    try:
        await set_rule(
            session, owner, kind=SenderRuleKind.ANONYMOUS_ADMIN,
            key=ANONYMOUS_ADMIN_BOT_ID, chat_id=None, side=SenderRuleSide.COMPANY,
            staff_id=person.id,
        )
    except ValueError:
        pass
    else:  # pragma: no cover — путь существует только при регрессии
        raise AssertionError("анонимный админ размечен сразу во всех чатах")


@requires_db
async def test_clearing_the_rule_brings_the_code_answer_back(session):
    """Отмена возвращает ответ КОДА, а не запомненное «как было»."""
    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -1009220006)
    person = await _staff(session, "Тамара Гришина")
    message = await _say(session, chat, 1, PERSON_TG, side=BusinessSide.CLIENT)

    await set_rule(
        session,
        owner,
        kind=SenderRuleKind.TG_USER,
        key=PERSON_TG,
        chat_id=None,
        side=SenderRuleSide.COMPANY,
        staff_id=person.id,
    )
    await session.refresh(message)
    assert message.business_side is BusinessSide.COMPANY

    rule = await load_rule(session, SenderRuleKind.TG_USER, PERSON_TG)
    stats = await clear_rule(session, owner, rule)

    assert stats["messages"] == 1
    await session.refresh(message)
    assert message.business_side is BusinessSide.CLIENT, "код снова решает сам"
    assert await load_rule(session, SenderRuleKind.TG_USER, PERSON_TG) is None
    left = await session.scalar(
        select(Attribution.message_id).where(Attribution.message_id == message.id)
    )
    assert left is None, "автор остался у сообщения клиента"


# ── Применение задним числом ─────────────────────────────────────────────


@requires_db
async def test_rule_rewrites_the_whole_history(session):
    """Стороны, вердикты, `needs_reclassification` и авторы — за всё время.

    Переворот клиент↔компания обязан стирать вердикт: у клиента спрашивают
    «нужен ли ответ», у компании — «по существу ли ответ», и старый вердикт
    получен не тем промптом.
    """
    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -1009220007)
    person = await _staff(session, "Дарья Лебедева")
    mine = [
        await _say(session, chat, index, PERSON_TG, side=BusinessSide.CLIENT, minutes=index)
        for index in range(1, 4)
    ]
    foreign = await _say(
        session, chat, 50, CLIENT_TG, side=BusinessSide.CLIENT, minutes=50
    )
    for message in (*mine, foreign):
        session.add(
            Classification(
                message_id=message.id,
                model=get_settings().ai_model,
                prompt_version=14,
                source="model",
                label="request",
                requires_response=True,
            )
        )
    await session.flush()

    _, stats = await set_rule(
        session,
        owner,
        kind=SenderRuleKind.TG_USER,
        key=PERSON_TG,
        chat_id=None,
        side=SenderRuleSide.COMPANY,
        staff_id=person.id,
        display="Дарья",
    )

    assert stats["messages"] == 3 and stats["changed"] == 3
    for message in mine:
        await session.refresh(message)
        assert message.business_side is BusinessSide.COMPANY
        assert message.needs_reclassification is True, "вердикт не переспросят"
    verdicts = set(
        (
            await session.scalars(
                select(Classification.message_id).where(
                    Classification.message_id.in_([m.id for m in mine])
                )
            )
        ).all()
    )
    assert not verdicts, "вердикт, полученный не тем промптом, остался действовать"

    authors = (
        await session.execute(
            select(Attribution.message_id, Attribution.staff_id, Attribution.method).where(
                Attribution.message_id.in_([m.id for m in mine])
            )
        )
    ).all()
    assert len(authors) == 3
    assert {row.staff_id for row in authors} == {person.id}
    assert {row.method for row in authors} == {AttributionMethod.MANUAL}

    # Соседа по чату решение не тронуло — ни стороны, ни вердикта.
    await session.refresh(foreign)
    assert foreign.business_side is BusinessSide.CLIENT
    assert await session.scalar(
        select(Classification.id).where(Classification.message_id == foreign.id)
    )

    # Журнал действий: решение, меняющее отчёты, обязано оставлять след.
    logged = (
        await session.scalars(
            select(AuditLog.action).where(AuditLog.object_type == "sender_rule")
        )
    ).all()
    assert "sender_rule.set" in logged


@requires_db
async def test_leaving_the_company_takes_the_author_away(session):
    """Ушло со стороны компании — строка автора уходит вместе с ним.

    Иначе сообщение звало бы в очередь «Не определили, кто это», где
    выбирать уже некого: у клиента и у системы автора-сотрудника нет.
    """
    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -1009220008)
    person = await _staff(session, "Анна Петрова")
    message = await _say(session, chat, 1, PERSON_TG, side=BusinessSide.CLIENT)

    await set_rule(
        session, owner, kind=SenderRuleKind.TG_USER, key=PERSON_TG,
        chat_id=None, side=SenderRuleSide.COMPANY, staff_id=person.id,
    )
    assert await session.scalar(
        select(Attribution.staff_id).where(Attribution.message_id == message.id)
    ) == person.id

    await set_rule(
        session, owner, kind=SenderRuleKind.TG_USER, key=PERSON_TG,
        chat_id=None, side=SenderRuleSide.SYSTEM,
    )
    await session.refresh(message)
    assert message.business_side is BusinessSide.INTEGRATOR_SYSTEM
    assert await session.scalar(
        select(Attribution.message_id).where(Attribution.message_id == message.id)
    ) is None


@requires_db
async def test_rule_survives_a_code_side_recount(session):
    """Пересчёт по версии правила КОДА ручное решение не затирает.

    Правило — данные, а не версия кода, и `recompute_sides` идёт через
    ту же `resolve_business_side`.
    """
    from app.services.reprocess import recompute_sides

    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -1009220009)
    person = await _staff(session, "Пётр Смирнов")
    message = await _say(session, chat, 1, PERSON_TG, side=BusinessSide.CLIENT)

    await set_rule(
        session, owner, kind=SenderRuleKind.TG_USER, key=PERSON_TG,
        chat_id=None, side=SenderRuleSide.COMPANY, staff_id=person.id,
    )
    await recompute_sides(session)

    await session.refresh(message)
    assert message.business_side is BusinessSide.COMPANY, "пересчёт стёр решение человека"
    row = await session.scalar(
        select(Attribution).where(Attribution.message_id == message.id)
    )
    assert row is not None and row.staff_id == person.id
    assert row.method is AttributionMethod.MANUAL


@requires_db
async def test_service_events_are_never_touched_by_a_rule(session):
    """«Участник вошёл в группу» не становится сообщением компании.

    У служебного события тоже есть отправитель, и без отдельной страховки
    правило превращало бы его в человеческую активность — ложное обращение.
    """
    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -1009220010)
    service = Message(
        chat_id=chat.id, tg_message_id=1, tg_user_id=PERSON_TG,
        transport_actor_kind=TransportActorKind.TELEGRAM_SYSTEM,
        business_side=BusinessSide.UNKNOWN, text=None, char_count=0,
        media_kind="service", sent_at=BASE,
    )
    session.add(service)
    await session.flush()

    _, stats = await set_rule(
        session, owner, kind=SenderRuleKind.TG_USER, key=PERSON_TG,
        chat_id=None, side=SenderRuleSide.COMPANY,
    )

    assert stats["messages"] == 0
    await session.refresh(service)
    assert service.business_side is BusinessSide.UNKNOWN
    assert await resolve_business_side(
        session, TransportActorKind.TELEGRAM_SYSTEM, PERSON_TG, None, chat_id=chat.id
    ) is BusinessSide.UNKNOWN


# ── Очередь ──────────────────────────────────────────────────────────────


@requires_db
async def test_queue_shows_bots_without_a_rule_only(session):
    """Бот с решением уходит из очереди; анонимный админ в неё не попадает."""
    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -1009220011)
    await _say(
        session, chat, 1, BOT_TG, side=BusinessSide.UNKNOWN,
        actor=TransportActorKind.OTHER_BOT, text="Заявка принята",
    )
    await _say(
        session, chat, 2, ANONYMOUS_ADMIN_BOT_ID, side=BusinessSide.COMPANY,
        actor=TransportActorKind.OTHER_BOT, text="Проведено", minutes=1,
    )

    listed = {row["tg_user_id"] for row in await unruled_bot_senders(session)}
    assert BOT_TG in listed
    assert ANONYMOUS_ADMIN_BOT_ID not in listed, "анонимного админа разбирают из переписки"

    await set_rule(
        session, owner, kind=SenderRuleKind.BOT, key=BOT_TG,
        chat_id=None, side=SenderRuleSide.SYSTEM,
    )
    assert BOT_TG not in {row["tg_user_id"] for row in await unruled_bot_senders(session)}


# ── Экраны ───────────────────────────────────────────────────────────────


class _FakeMessage:
    def __init__(self) -> None:
        self.text: str | None = None
        self.markup = None

    async def edit_text(self, text, reply_markup=None, parse_mode=None, **kwargs):
        self.text = text
        self.markup = reply_markup


class _FakeQuery:
    def __init__(self) -> None:
        self.message = _FakeMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", **kwargs):
        self.answers.append(text)


def _actions(query: _FakeQuery) -> set[str]:
    return {
        SenderAction.unpack(button.callback_data).action
        for row in query.message.markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("sr:")
    }


@requires_db
async def test_sender_card_offers_the_three_decisions(session, monkeypatch):
    """Карточка отвечает «кто это, откуда сторона» и даёт три кнопки решения."""
    import app.bot.handlers.sender_ui as ui

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(ui, "session_scope", fake_scope)

    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -1009220012, "ИП Смирнов")
    await _staff(session, "Ирина Соколова")
    await _say(session, chat, 1, PERSON_TG, side=BusinessSide.CLIENT, text="Добрый день")
    await session.flush()

    query = _FakeQuery()
    await ui.on_card(query, SenderAction(action="card", key=PERSON_TG), owner)

    assert query.message.text and "Кто это?" in query.message.text
    assert "ИП Смирнов" in query.message.text, "карточка не говорит, где он писал"
    assert "автоматически" in query.message.text, "не объяснено, откуда взялась сторона"
    actions = _actions(query)
    assert {"roles", "cli", "sys"} <= actions
    assert "del" not in actions, "нечего сбрасывать — решения ещё нет"

    await set_rule(
        session, owner, kind=SenderRuleKind.TG_USER, key=PERSON_TG,
        chat_id=None, side=SenderRuleSide.CLIENT,
    )
    decided = _FakeQuery()
    await ui.on_card(decided, SenderAction(action="card", key=PERSON_TG), owner)
    assert "del" in _actions(decided), "решение нельзя отменить"
    assert "по правилу" in decided.message.text


@requires_db
async def test_card_without_the_permission_is_read_only(session, monkeypatch):
    """Без права разметки карточка открывается, но решать нечем."""
    import app.bot.handlers.sender_ui as ui

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(ui, "session_scope", fake_scope)

    viewer = _owner(role=BotRole.MANAGER, tg_user_id=880901)
    session.add(viewer)
    chat = await _chat(session, -1009220013)
    await _say(session, chat, 1, PERSON_TG, side=BusinessSide.CLIENT)
    await session.flush()

    query = _FakeQuery()
    await ui.on_card(query, SenderAction(action="card", key=PERSON_TG), viewer)

    assert query.message.text, "карточка не открылась вовсе"
    assert not (_actions(query) & {"roles", "cli", "sys", "del"})

    # И само решение мимо экрана тоже не проходит.
    decide = _FakeQuery()
    await ui.on_decide(decide, SenderAction(action="cli", key=PERSON_TG), viewer)
    assert decide.answers == ["Недостаточно прав"]
    assert await load_rule(session, SenderRuleKind.TG_USER, PERSON_TG) is None


@requires_db
async def test_service_refuses_a_decision_without_the_permission(session):
    """Право проверяется в сервисе, а не только на экране."""
    viewer = _owner(role=BotRole.MANAGER, tg_user_id=880902)
    session.add(viewer)
    await session.flush()

    try:
        await set_rule(
            session, viewer, kind=SenderRuleKind.TG_USER, key=PERSON_TG,
            chat_id=None, side=SenderRuleSide.CLIENT,
        )
    except AccessError:
        pass
    else:  # pragma: no cover — путь существует только при регрессии
        raise AssertionError("решение прошло без права attribution.assign")


@requires_db
async def test_participants_of_the_shown_window(session):
    """«Участники» показывают всех, кроме интегратора с разобранной подписью."""
    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -1009220014)
    person = await _staff(session, "Вера Павлова")

    client = await _say(session, chat, 1, CLIENT_TG, side=BusinessSide.CLIENT, text="Вопрос")
    await _say(
        session, chat, 2, ANONYMOUS_ADMIN_BOT_ID, side=BusinessSide.COMPANY,
        actor=TransportActorKind.OTHER_BOT, text="Приняли", minutes=1,
    )
    await _say(
        session, chat, 3, BOT_TG, side=BusinessSide.UNKNOWN,
        actor=TransportActorKind.OTHER_BOT, text="Рассылка", minutes=2,
    )
    known = await _say(
        session, chat, 4, 999001, side=BusinessSide.COMPANY,
        actor=TransportActorKind.INTEGRATOR_BOT, minutes=3,
        text="Вера Павлова [corp.example.com] пишет:\n\nСделано",
    )
    unknown = await _say(
        session, chat, 5, 999001, side=BusinessSide.COMPANY,
        actor=TransportActorKind.INTEGRATOR_BOT, minutes=4,
        text="Р. Бот-рассылка [corp.example.com] пишет:\n\nНалоги",
    )
    session.add(
        Attribution(message_id=known.id, staff_id=person.id,
                    method=AttributionMethod.EXACT, raw_name="Вера Павлова")
    )
    session.add(Attribution(message_id=unknown.id, raw_name="Р. Бот-рассылка"))
    await session.flush()

    rows = await window_participants(session, chat.id, client.id, unknown.id)
    labels = [row["label"] for row in rows]

    keys = {row.get("key") for row in rows if row["kind"] == "sender"}
    assert keys == {CLIENT_TG, ANONYMOUS_ADMIN_BOT_ID, BOT_TG}
    assert any("клиент" in label for label in labels)
    assert any("компания (анонимно)" in label for label in labels)
    assert any(f"бот {BOT_TG}" in label for label in labels)
    signatures = [row for row in rows if row["kind"] == "signature"]
    assert [row["raw_name"] for row in signatures] == ["Р. Бот-рассылка"]
    assert not any("Вера" in label for label in labels), (
        "сотрудник с разобранной подписью попал в список"
    )

    # Решение про отправителя видно в подписи — иначе список не отличает
    # «бот так решил» от «мы так решили».
    await set_rule(
        session, owner, kind=SenderRuleKind.ANONYMOUS_ADMIN,
        key=ANONYMOUS_ADMIN_BOT_ID, chat_id=chat.id,
        side=SenderRuleSide.COMPANY, staff_id=person.id,
    )
    updated = await window_participants(session, chat.id, client.id, unknown.id)
    assert any(
        "Вера Павлова (сотрудник, по правилу)" in row["label"] for row in updated
    )


@requires_db
async def test_auth_reply_hints_who_the_anonymous_admin_is(session):
    """Подсказка по «/auth»: чат подключают под своей учёткой, и Битрикс
    называет имя вслух. Правило она НЕ создаёт — только экономит два нажатия.
    """
    chat = await _chat(session, -1009220015)
    person = await _staff(session, "Вера Павлова")
    await _say(
        session, chat, 1, 999001, side=BusinessSide.INTEGRATOR_SYSTEM,
        actor=TransportActorKind.INTEGRATOR_BOT,
        text="@GroupAnonymousBot Вы авторизованы на corp.example.com как Вера Павлова",
    )

    found = await auth_hint(session, chat.id)

    assert found is not None
    staff, seen_at = found
    assert staff.id == person.id
    assert seen_at == BASE
    assert await session.scalar(select(SenderRule.id)) is None, "подсказка завела правило"

    # Имени нет в справочнике — подсказки нет: приписать наугад хуже.
    other = await _chat(session, -1009220016, "Другой")
    await _say(
        session, other, 1, 999001, side=BusinessSide.INTEGRATOR_SYSTEM,
        actor=TransportActorKind.INTEGRATOR_BOT,
        text="Вы авторизованы на corp.example.com как Неизвестный Человек",
    )
    assert await auth_hint(session, other.id) is None
