"""Срезы проваливания из отчёта: число на кнопке считается тем же условием,
что и список внутри; «Ждут специалиста» не показывает обращения из чатов
на паузе, как и алерты."""

from datetime import datetime, timedelta, timezone

from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.report_drill import (
    KIND_BREACH,
    KIND_BREACH_REACTION,
    KIND_BREACH_SPECIALIST,
    KIND_HANDOFF,
    KIND_NO_ANSWER,
    KIND_NO_NEED,
    KIND_WAITING,
    drill_counts,
    drill_page,
)
from app.services.tracking import open_period
from tests.conftest import requires_db

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
START = NOW - timedelta(days=7)
END = NOW + timedelta(days=1)


async def _chat(session, tg_chat_id: int, state: ChatState, title: str) -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title=title, state=state)
    session.add(chat)
    await session.flush()
    if state is ChatState.TRACKED:
        await open_period(session, chat, reason="test", at=START - timedelta(days=1))
    return chat


async def _interaction(session, chat: Chat, *, state, opened_delta_h: int, **extra) -> Interaction:
    opened_at = NOW - timedelta(hours=opened_delta_h)
    message = Message(
        chat_id=chat.id,
        tg_message_id=opened_delta_h + 1,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Вопрос по акту",
        char_count=14,
        sent_at=opened_at,
    )
    session.add(message)
    await session.flush()
    interaction = Interaction(
        chat_id=chat.id,
        opened_at=opened_at,
        opened_by_message_id=message.id,
        last_client_at=opened_at,
        client_messages=1,
        state=state,
        **extra,
    )
    session.add(interaction)
    await session.flush()
    return interaction


@requires_db
async def test_counts_match_lists(session):
    """Число для кнопки и total списка — одно и то же условие."""
    chat = await _chat(session, -100900101, ChatState.TRACKED, "Ромашка")

    await _interaction(session, chat, state=InteractionState.ANSWERED, opened_delta_h=1,
                       sla_breached=True)
    await _interaction(session, chat, state=InteractionState.ANSWERED, opened_delta_h=2,
                       substantive_breached=True)
    # Оба флага сразу — считается ОДНИМ обращением, не двумя.
    await _interaction(session, chat, state=InteractionState.ABANDONED, opened_delta_h=3,
                       sla_breached=True, substantive_breached=True)
    await _interaction(session, chat, state=InteractionState.REACTED, opened_delta_h=4,
                       handoff_at=NOW - timedelta(hours=4))
    await _interaction(session, chat, state=InteractionState.ANSWERED, opened_delta_h=5)

    counts = await drill_counts(session, START, END)
    assert counts[KIND_BREACH] == 3
    assert counts[KIND_HANDOFF] == 1
    assert counts[KIND_NO_ANSWER] == 1

    for kind in (KIND_BREACH, KIND_HANDOFF, KIND_NO_ANSWER):
        _, total = await drill_page(session, kind, START, END)
        assert total == counts[kind], f"{kind}: кнопка и список разошлись"


@requires_db
async def test_handoff_hides_paused_chats(session):
    """Пауза чата глушит и очередь «ждут специалиста», как глушит алерты."""
    active = await _chat(session, -100900102, ChatState.TRACKED, "Активный")
    paused = await _chat(session, -100900103, ChatState.PAUSED, "На паузе")

    await _interaction(session, active, state=InteractionState.REACTED, opened_delta_h=2,
                       handoff_at=NOW - timedelta(hours=2))
    await _interaction(session, paused, state=InteractionState.REACTED, opened_delta_h=2,
                       handoff_at=NOW - timedelta(hours=2))

    counts = await drill_counts(session, START, END)
    assert counts[KIND_HANDOFF] == 1
    items, _ = await drill_page(session, KIND_HANDOFF, START, END)
    assert [item["title"] for item in items] == ["Активный"]


@requires_db
async def test_no_answer_is_not_hidden_by_pause(session):
    """«Осталось без ответа» — история периода: пауза её не стирает."""
    paused = await _chat(session, -100900104, ChatState.PAUSED, "На паузе")
    await _interaction(session, paused, state=InteractionState.ABANDONED, opened_delta_h=6)

    counts = await drill_counts(session, START, END)
    assert counts[KIND_NO_ANSWER] == 1


@requires_db
async def test_chat_filter_narrows_every_kind(session):
    """Срез из отчёта по чату видит только этот чат."""
    first = await _chat(session, -100900105, ChatState.TRACKED, "Первый")
    second = await _chat(session, -100900106, ChatState.TRACKED, "Второй")

    await _interaction(session, first, state=InteractionState.ABANDONED, opened_delta_h=1)
    await _interaction(session, second, state=InteractionState.ABANDONED, opened_delta_h=2)

    all_counts = await drill_counts(session, START, END)
    assert all_counts[KIND_NO_ANSWER] == 2
    one_chat = await drill_counts(session, START, END, chat_id=first.id)
    assert one_chat[KIND_NO_ANSWER] == 1
    items, total = await drill_page(session, KIND_NO_ANSWER, START, END, chat_id=first.id)
    assert total == 1 and items[0]["title"] == "Первый"


@requires_db
async def test_period_bounds_respected(session):
    """Обращение вне периода в срез не попадает — период наследуется из отчёта."""
    chat = await _chat(session, -100900107, ChatState.TRACKED, "Ромашка")
    await _interaction(session, chat, state=InteractionState.ABANDONED,
                       opened_delta_h=24 * 30)  # месяц назад — вне периода

    counts = await drill_counts(session, START, END)
    assert counts[KIND_NO_ANSWER] == 0


@requires_db
async def test_no_need_carries_quote_and_verdict(session):
    """«Ответ не требовался» показывает цитату и КТО решил."""
    from app.config import get_settings
    from app.db.models import Classification

    model = get_settings().ai_model
    chat = await _chat(session, -100900109, ChatState.TRACKED, "Ромашка")
    by_rule = await _interaction(
        session, chat, state=InteractionState.NO_RESPONSE_NEEDED, opened_delta_h=1
    )
    by_model = await _interaction(
        session, chat, state=InteractionState.NO_RESPONSE_NEEDED, opened_delta_h=2
    )
    session.add_all(
        [
            Classification(
                message_id=by_rule.opened_by_message_id,
                model=model,
                prompt_version=2,
                source="rule",
                label="ack",
                requires_response=False,
            ),
            Classification(
                message_id=by_model.opened_by_message_id,
                model=model,
                prompt_version=2,
                source="model",
                label="info",
                requires_response=False,
            ),
        ]
    )
    await session.flush()

    counts = await drill_counts(session, START, END)
    assert counts[KIND_NO_NEED] == 2

    items, total = await drill_page(session, KIND_NO_NEED, START, END)
    assert total == 2
    by_id = {item["opened_by_message_id"]: item for item in items}
    assert by_id[by_rule.opened_by_message_id]["verdict_source"] == "rule"
    assert by_id[by_model.opened_by_message_id]["verdict_source"] == "model"
    assert by_id[by_model.opened_by_message_id]["verdict_label"] == "info"
    assert all(item["opener_text"] == "Вопрос по акту" for item in items)


@requires_db
async def test_latest_prompt_version_and_active_model_win(session):
    """Причина берётся от последней версии промпта активной модели.

    У сообщения вердикты обеих версий плюс вердикт чужой модели —
    показывается ровно тот, по которому построено обращение.
    """
    from app.config import get_settings
    from app.db.models import Classification

    model = get_settings().ai_model
    chat = await _chat(session, -100900110, ChatState.TRACKED, "Ромашка")
    interaction = await _interaction(
        session, chat, state=InteractionState.NO_RESPONSE_NEEDED, opened_delta_h=1
    )
    session.add_all(
        [
            Classification(
                message_id=interaction.opened_by_message_id,
                model=model,
                prompt_version=1,
                source="model",
                label="info",
            ),
            Classification(
                message_id=interaction.opened_by_message_id,
                model=model,
                prompt_version=2,
                source="rule",
                label="ack",
            ),
            # Чужая модель с самой высокой версией — игнорируется.
            Classification(
                message_id=interaction.opened_by_message_id,
                model="candidate/other-model",
                prompt_version=9,
                source="model",
                label="social",
            ),
        ]
    )
    await session.flush()

    items, _ = await drill_page(session, KIND_NO_NEED, START, END)
    assert items[0]["verdict_source"] == "rule"
    assert items[0]["verdict_label"] == "ack"


@requires_db
async def test_handoff_queue_oldest_first(session):
    """«Ждут специалиста» — очередь: дольше всех ждущий сверху."""
    chat = await _chat(session, -100900108, ChatState.TRACKED, "Ромашка")
    await _interaction(session, chat, state=InteractionState.REACTED, opened_delta_h=1,
                       handoff_at=NOW - timedelta(hours=1))
    await _interaction(session, chat, state=InteractionState.REACTED, opened_delta_h=5,
                       handoff_at=NOW - timedelta(hours=5))

    items, _ = await drill_page(session, KIND_HANDOFF, START, END)
    assert items[0]["handoff_at"] < items[1]["handoff_at"]


# ═══════════════════════════════════════════════════════════════
# Просрочки разделены по видам
# ═══════════════════════════════════════════════════════════════


@requires_db
async def test_breach_slices_split_by_who_is_late(session):
    """«Просрочено» — это две разные проблемы, и смотреть их надо порознь.

    Нет первого отклика (порог 30 минут) и специалист не ответил на
    переданное (порог сутки) — разные виновники и разные сроки.
    """
    chat = await _chat(session, -100900190, ChatState.TRACKED, "Просрочки")

    await _interaction(
        session, chat, state=InteractionState.ANSWERED, opened_delta_h=1, sla_breached=True
    )
    await _interaction(
        session,
        chat,
        state=InteractionState.ANSWERED,
        opened_delta_h=2,
        substantive_breached=True,
        handoff_at=NOW - timedelta(hours=2),
    )

    counts = await drill_counts(session, START, END, chat.id)

    assert counts[KIND_BREACH_REACTION] == 1, "срез «никто не отреагировал»"
    assert counts[KIND_BREACH_SPECIALIST] == 1, "срез «специалист не ответил»"
    # Общий срез остаётся суммой обоих: кнопки старых сообщений работают.
    assert counts[KIND_BREACH] == 2

    rows, total = await drill_page(session, KIND_BREACH_REACTION, START, END, chat.id)
    assert total == 1
    assert rows[0]["sla_breached"] is True
    assert rows[0]["substantive_breached"] is False


@requires_db
async def test_waiting_slice_shows_those_still_without_answer(session):
    """Обращения, которым ещё не ответили, видны в своём срезе."""
    chat = await _chat(session, -100900191, ChatState.TRACKED, "Ожидание")

    # Никто не откликнулся.
    await _interaction(session, chat, state=InteractionState.OPEN, opened_delta_h=5)
    # Откликнулись, но ответа по существу нет и передачи не было.
    await _interaction(session, chat, state=InteractionState.REACTED, opened_delta_h=2)
    # Передача была — это уже другой срез.
    await _interaction(
        session,
        chat,
        state=InteractionState.REACTED,
        opened_delta_h=3,
        handoff_at=NOW - timedelta(hours=2),
    )
    # Отвечено — в очереди ожидания ему делать нечего.
    await _interaction(session, chat, state=InteractionState.ANSWERED, opened_delta_h=4)

    counts = await drill_counts(session, START, END, chat.id)
    rows, total = await drill_page(session, KIND_WAITING, START, END, chat.id)

    assert counts[KIND_WAITING] == 2, "в ожидании должны быть двое"
    assert counts[KIND_HANDOFF] == 1, "передача считается отдельно"
    assert total == 2
    # Очередь: дольше всех ждущий — сверху.
    assert rows[0]["opened_at"] < rows[1]["opened_at"]


@requires_db
async def test_waiting_slice_is_silenced_by_pausing_the_chat(session):
    """Пауза чата глушит ожидание — как и в алертах."""
    chat = await _chat(session, -100900192, ChatState.PAUSED, "На паузе")
    await _interaction(session, chat, state=InteractionState.OPEN, opened_delta_h=5)

    counts = await drill_counts(session, START, END, chat.id)

    assert counts[KIND_WAITING] == 0


# ═══════════════════════════════════════════════════════════════
# Навигация: «Назад» ведёт туда, откуда провалились.
# Срез «Осталось без ответа» открывается с ДВУХ экранов — из сводного
# отчёта и из «Требует внимания». Происхождение едет сентинелом в chat_id
# (новое поле в схеме callback сломало бы кнопки старых сообщений).
# ═══════════════════════════════════════════════════════════════

from contextlib import asynccontextmanager  # noqa: E402

from app.bot.callbacks import (  # noqa: E402
    DRILL_FROM_ATTENTION,
    DrillAction,
    LabAction,
    ReportAction,
)
from app.db.models import BotRole, BotUser, BotUserState  # noqa: E402


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


async def _open_drill(session, monkeypatch, drill_chat_id: int, fill_pages: bool = False):
    import app.bot.handlers.reports as reports

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(reports, "session_scope", fake_scope)
    if fill_pages:
        # Две страницы синтетики: пагинация появляется только при total
        # больше страницы, а нас интересуют её callback data, не данные.
        opened = datetime.now(timezone.utc) - timedelta(hours=2)
        items = [
            {
                "title": f"Чат {n}",
                "opened_at": opened,
                "client_messages": 1,
                "first_reaction_at": None,
                "chat_id": 1,
                "opened_by_message_id": 1,
            }
            for n in range(6)
        ]

        async def fake_page(session_, kind, start, end, chat_id, page):
            return items, len(items) * 2

        monkeypatch.setattr(reports, "drill_page", fake_page)

    owner = BotUser(
        tg_user_id=770100, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
    )
    query = _FakeQuery()
    await reports.on_drill(
        query,
        DrillAction(kind=KIND_NO_ANSWER, period="last7", chat_id=drill_chat_id),
        owner,
    )
    return query


def _buttons(query):
    return [
        button
        for row in query.message.markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


@requires_db
async def test_drill_from_attention_returns_to_attention(session, monkeypatch):
    """Из «Требует внимания» возврат ведёт обратно туда, а не в сводный отчёт."""
    query = await _open_drill(session, monkeypatch, DRILL_FROM_ATTENTION)

    lab_targets = [
        LabAction.unpack(b.callback_data).kind
        for b in _buttons(query)
        if b.callback_data.startswith("lab:")
    ]
    assert lab_targets == ["attention"], "возврат обязан вести в «Требует внимания»"
    assert not any(
        b.callback_data.startswith("rep:") for b in _buttons(query)
    ), "кнопки в отчёт из среза внимания быть не должно"


@requires_db
async def test_drill_from_report_returns_to_report(session, monkeypatch):
    """Инвариант второго входа: из отчёта — обратно в отчёт."""
    query = await _open_drill(session, monkeypatch, 0)

    back = [b for b in _buttons(query) if b.callback_data.startswith("rep:")]
    assert len(back) == 1
    action = ReportAction.unpack(back[0].callback_data)
    assert (action.action, action.scope, action.period) == ("run", "all", "last7")
    assert not any(b.callback_data.startswith("lab:") for b in _buttons(query))


@requires_db
async def test_drill_pagination_keeps_the_origin(session, monkeypatch):
    """Перелистнул страницу — возврат всё ещё помнит, откуда пришли."""
    query = await _open_drill(
        session, monkeypatch, DRILL_FROM_ATTENTION, fill_pages=True
    )

    nav = [
        DrillAction.unpack(b.callback_data)
        for b in _buttons(query)
        if b.callback_data.startswith("dr:")
    ]
    assert nav, "при двух страницах обязана быть кнопка листания"
    assert all(a.chat_id == DRILL_FROM_ATTENTION for a in nav), (
        "листание потеряло происхождение — после него «Назад» уведёт в отчёт"
    )
