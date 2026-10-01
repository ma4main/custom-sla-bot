"""Очередь «Не узнали автора»: «не сотрудник» убирает подпись и переживает
пересчёт, решение отменяемо, привязка накрывает всю группу подписи, новое
написание в справочнике разбирает уже накопленные сообщения."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.bot.callbacks import MarkupAction

from app.db.models import (
    Attribution,
    AttributionMethod,
    BotRole,
    BotUser,
    BotUserState,
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    Message,
    Staff,
    TransportActorKind,
)
from app.services.episodes import rebuild_interactions
from app.services.tracking import open_period
from app.services.attribution import (
    attribute_all,
    bind_raw_name,
    count_not_staff,
    count_unresolved,
    group_size,
    mark_not_staff,
    not_staff_groups,
    restore_to_queue,
    unresolved_groups,
)
from app.services.staff import add_alias, list_staff, normalize_name
from app.services.staff_roles import ROLE_MANAGER, ROLE_SPECIALIST, ROLE_UNDECIDED
from tests.conftest import requires_db

BASE = datetime(2026, 8, 20, 10, tzinfo=timezone.utc)


def _owner() -> BotUser:
    return BotUser(
        tg_user_id=770001, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
    )


async def _chat(session, tg_chat_id: int = -100777001) -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title="Разметка", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    return chat


async def _messages(session, chat: Chat, signature: str, count: int, *, first_id: int = 1):
    for index in range(count):
        session.add(
            Message(
                chat_id=chat.id,
                tg_message_id=first_id + index,
                transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
                business_side=BusinessSide.COMPANY,
                text=f"{signature} [corp.example.com] пишет:\n\nтекст {index}",
                char_count=30,
                sent_at=BASE + timedelta(minutes=first_id + index),
            )
        )
    await session.flush()


@requires_db
async def test_not_staff_leaves_the_queue_and_survives_reattribution(session):
    """«Система» уходит из очереди — и не возвращается следующим проходом."""
    owner = _owner()
    session.add(owner)
    chat = await _chat(session)
    await _messages(session, chat, "Система", 3)
    await attribute_all(session)

    assert await count_unresolved(session) == 3

    groups = await unresolved_groups(session)
    assert [(name, count) for name, count, _ in groups] == [("Система", 3)]
    anchor = groups[0][2]
    assert await group_size(session, anchor) == 3

    marked = await mark_not_staff(session, owner, anchor)

    assert marked == 3
    assert await count_unresolved(session) == 0, "подпись осталась в очереди"
    assert await count_not_staff(session) == 3

    # Пересчёт атрибуции решение человека не отменяет.
    await attribute_all(session)

    assert await count_unresolved(session) == 0
    assert await count_not_staff(session) == 3


@requires_db
async def test_not_staff_decision_covers_future_messages(session):
    """Решение относится к подписи, а не к разобранным строкам: новый выпуск
    рассылки в очередь не возвращается."""
    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -100777006)
    await _messages(session, chat, "Система", 1)
    await attribute_all(session)

    anchor = (await unresolved_groups(session))[0][2]
    await mark_not_staff(session, owner, anchor)
    assert await count_unresolved(session) == 0

    # Пришёл следующий выпуск рассылки.
    await _messages(session, chat, "Система", 1, first_id=50)
    await attribute_all(session)

    assert await count_unresolved(session) == 0, "новая рассылка вернулась в очередь"
    assert await count_not_staff(session) == 2

    # А вот подпись, про которую решения не было, в очередь попасть обязана.
    await _messages(session, chat, "Р. Бот-рассылка", 1, first_id=70)
    await attribute_all(session)

    assert await count_unresolved(session) == 1


@requires_db
async def test_not_staff_is_reversible(session):
    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -100777002)
    await _messages(session, chat, "Система", 2)
    await attribute_all(session)

    anchor = (await unresolved_groups(session))[0][2]
    await mark_not_staff(session, owner, anchor)
    assert await count_unresolved(session) == 0

    back = (await not_staff_groups(session))[0][2]
    restored = await restore_to_queue(session, owner, back)

    assert restored == 2
    assert await count_unresolved(session) == 2, "подпись не вернулась в очередь"
    assert await count_not_staff(session) == 0


@requires_db
async def test_bind_covers_the_whole_signature_group(session):
    """Решение принимается один раз — применяется ко всем сообщениям подписи."""
    owner = _owner()
    session.add(owner)
    person = Staff(
        full_name="Ирина Соколова", normalized_name=normalize_name("Ирина Соколова")
    )
    session.add(person)
    chat = await _chat(session, -100777003)
    # Подпись с инициалом: строгую форму «Имя Фамилия» она не проходит,
    # поэтому сотрудник по ней не заводится сам — и она ждёт человека.
    await _messages(session, chat, "И. Соколова", 4)
    await attribute_all(session)
    await session.flush()

    anchor = (await unresolved_groups(session))[0][2]
    count = await bind_raw_name(session, owner, anchor, person.id)

    assert count == 4
    assert await count_unresolved(session) == 0
    rows = (
        await session.scalars(select(Attribution).where(Attribution.staff_id == person.id))
    ).all()
    assert len(rows) == 4
    assert {row.method for row in rows} == {AttributionMethod.MANUAL}
    assert {row.assigned_by for row in rows} == {owner.id}


@requires_db
async def test_new_spelling_picks_up_old_unresolved_messages(session):
    """Новое написание имени разбирает уже накопленные нераспознанные сообщения."""
    owner = _owner()
    session.add(owner)
    person = Staff(
        full_name="Тамара Гришина", normalized_name=normalize_name("Тамара Гришина")
    )
    session.add(person)
    chat = await _chat(session, -100777004)
    await _messages(session, chat, "Т. Гришина", 5)
    await attribute_all(session)
    await session.flush()

    assert await count_unresolved(session) == 5, "сообщения должны ждать в очереди"

    await add_alias(session, owner, person, "Т. Гришина")
    await attribute_all(session)

    assert await count_unresolved(session) == 0, "прежние сообщения не подхватились"
    rows = (
        await session.scalars(select(Attribution).where(Attribution.staff_id == person.id))
    ).all()
    assert len(rows) == 5


@requires_db
async def test_manual_decisions_are_never_overwritten_by_recalculation(session):
    """Ручная привязка сильнее автоматики — даже если появился однофамилец."""
    owner = _owner()
    session.add(owner)
    chosen = Staff(full_name="Дарья Лебедева", normalized_name=normalize_name("Дарья Лебедева"))
    session.add(chosen)
    chat = await _chat(session, -100777005)
    await _messages(session, chat, "Д. Лебедева", 2)
    await attribute_all(session)
    await session.flush()

    anchor = (await unresolved_groups(session))[0][2]
    await bind_raw_name(session, owner, anchor, chosen.id)

    await attribute_all(session)

    rows = (await session.scalars(select(Attribution).where(Attribution.raw_name == "Д. Лебедева"))).all()
    assert {row.staff_id for row in rows} == {chosen.id}


class _FakeMessage:
    """Ловушка для того, что обработчик отрисовал."""

    def __init__(self) -> None:
        self.text: str | None = None
        self.markup = None

    async def edit_text(self, text, reply_markup=None, parse_mode=None, **kwargs):
        self.text = text
        self.markup = reply_markup


class _FakeQuery:
    def __init__(self) -> None:
        self.message = _FakeMessage()

    async def answer(self, *args, **kwargs):
        return None


@requires_db
async def test_looking_at_a_signature_does_not_undo_the_decision(session, monkeypatch):
    """Открыть подпись и отменить решение — разные действия.

    Строка списка ведёт в карточку, а возврат в очередь — отдельная кнопка на ней.
    """
    import app.bot.handlers.staff_ui as ui

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(ui, "session_scope", fake_scope)

    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -100777008)
    await _messages(session, chat, "Система", 2)
    await attribute_all(session)

    anchor = (await unresolved_groups(session))[0][2]
    await mark_not_staff(session, owner, anchor)
    await session.flush()

    query = _FakeQuery()
    await ui._render_ignored(query)

    actions = {
        MarkupAction.unpack(button.callback_data).action
        for row in query.message.markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("mk:")
    }

    assert "iview" in actions, "строка списка должна открывать подпись"
    assert "restore" not in actions, "строка списка сразу отменяет решение"
    assert await count_not_staff(session) == 2, "просмотр списка изменил данные"

    # Обработчик карточки доживает до ответа.
    card = _FakeQuery()
    await ui.on_markup_ignored_view(
        card, MarkupAction(action="iview", msg_id=anchor), owner
    )

    assert card.message.text, "карточка подписи не отрисовалась"
    assert "Система" in card.message.text
    card_actions = {
        MarkupAction.unpack(button.callback_data).action
        for row in card.message.markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("mk:")
    }
    assert "restore" in card_actions, "с карточки нельзя вернуть подпись в очередь"
    assert await count_not_staff(session) == 2, "просмотр карточки изменил данные"


@requires_db
async def test_mailout_does_not_answer_for_a_human(session):
    """Рассылка робота не закрывает обращение и не крадёт заслугу.

    Рассылка «Система» уходит через 4 минуты после просьбы, менеджер
    отвечает через 17 секунд после неё: реакция и ответ — за менеджером.
    """
    owner = _owner()
    session.add(owner)
    person = Staff(
        full_name="Вера Павлова",
        normalized_name=normalize_name("Вера Павлова"),
    )
    session.add(person)
    chat = await _chat(session, -100777007)
    await open_period(session, chat, reason="test", at=BASE - timedelta(days=1))

    opened_at = datetime(2026, 8, 27, 5, 56, tzinfo=timezone.utc)  # 08:56 МСК
    session.add(
        Message(
            chat_id=chat.id,
            tg_message_id=901,
            transport_actor_kind=TransportActorKind.HUMAN_USER,
            business_side=BusinessSide.CLIENT,
            text="Вера, добрый день. Позвоните ещё раз Диане, она ждёт звонка",
            char_count=60,
            sent_at=opened_at,
        )
    )
    session.add(
        Message(
            chat_id=chat.id,
            tg_message_id=902,
            transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
            business_side=BusinessSide.COMPANY,
            text="Система [corp.example.com] пишет:\n\nДоброе утро.\nНалоги к оплате до 28.08.",
            char_count=70,
            sent_at=opened_at + timedelta(minutes=4, seconds=2),
        )
    )
    human = Message(
        chat_id=chat.id,
        tg_message_id=903,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY,
        text="Вера Павлова [corp.example.com] пишет:\n\nПо Ромашке принято",
        char_count=50,
        sent_at=opened_at + timedelta(minutes=4, seconds=19),
    )
    session.add(human)
    await session.flush()

    await attribute_all(session)
    anchor = (await unresolved_groups(session))[0][2]
    await mark_not_staff(session, owner, anchor)
    await session.flush()

    await rebuild_interactions(session, now=opened_at + timedelta(hours=1))
    await session.flush()

    episode = await session.scalar(select(Interaction).where(Interaction.chat_id == chat.id))
    assert episode.first_reaction_at == human.sent_at, "реакцией засчитана рассылка"
    assert episode.first_reaction_staff_id == person.id, "заслуга ушла в никуда"
    assert episode.substantive_staff_id == person.id


@requires_db
async def test_undecided_slice_shows_people_without_role(session):
    """Группа «роль не определена» — это NULL в базе, а не отдельное значение."""
    for name, role in (
        ("Анна Специалист", ROLE_SPECIALIST),
        ("Борис Менеджер", ROLE_MANAGER),
        ("Вера Безроли", None),
        ("Глеб Безроли", None),
    ):
        session.add(
            Staff(full_name=name, normalized_name=normalize_name(name), role=role)
        )
    await session.flush()

    _, total = await list_staff(session, limit=1)
    _, specialists = await list_staff(session, limit=1, role=ROLE_SPECIALIST)
    _, managers = await list_staff(session, limit=1, role=ROLE_MANAGER)
    people, undecided = await list_staff(session, limit=10, role=ROLE_UNDECIDED)

    assert undecided == 2
    assert {person.full_name for person in people} == {"Вера Безроли", "Глеб Безроли"}
    # Инвариант раздела: три группы покрывают справочник целиком, и счётчики
    # на кнопках обязаны сходиться с общим числом.
    assert specialists + managers + undecided == total
