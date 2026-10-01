"""Синтетические причинные контексты на настоящем движке эпизодов, без модели."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from app.db.models import BusinessSide, Chat, ChatState, Message, TransportActorKind
from app.services.ai import OPEN_ITEMS, TAIL_EVENTS, company_payload
from app.services.classify_context import ContextBatch
from app.services.episodes import EpisodeMessage, EpisodeReplayInput
from app.services.settings_store import DEFAULTS
from tests.conftest import requires_db

T0 = datetime(2026, 9, 14, 9, tzinfo=timezone.utc)
CLIENT, COMPANY = BusinessSide.CLIENT, BusinessSide.COMPANY
HUMAN = TransportActorKind.HUMAN_USER
REQUEST = (True, None, "request", None)


def message(mid, side=CLIENT, *, seconds=None, text=None, media=None, thread=None, actor=HUMAN,
            author=None, reply_to=None):
    """`tg_message_id` равен нашему id, поэтому `reply_to` указывается тем же
    номером, что и сообщение. `author` по умолчанию неизвестен — как у сообщений
    без подписи, и правила по автору на нём не срабатывают.
    """
    return EpisodeMessage(
        id=mid, chat_id=1, thread_id=thread,
        sent_at=T0 + timedelta(seconds=mid if seconds is None else seconds),
        business_side=side, text=text, has_media=media is not None,
        media_kind=media, transport_actor_kind=actor,
        reply_to_tg_message_id=reply_to, tg_message_id=mid, tg_user_id=author,
    )


def batch(messages, verdicts=None, *, broadcasts=(), forum=False, shadow=False):
    snapshot = EpisodeReplayInput(
        chat_ids=(1,), messages_by_chat=MappingProxyType({1: tuple(messages)}),
        forum_map=MappingProxyType({1: forum}), attribution_map=MappingProxyType({}),
        not_staff_messages=frozenset(), verdicts=MappingProxyType(verdicts or {}),
        broadcast_rows=tuple(broadcasts),
        calendar_json=json.dumps(DEFAULTS["work_calendar"]),
        episodes_json=json.dumps(DEFAULTS["episodes"]),
        alerts_json=json.dumps(DEFAULTS["alerts"]),
        rules_since=(T0 - timedelta(days=1),) * 3,
        ai_shadow_mode=shadow,
    )
    return ContextBatch(snapshot)


def ids(context):
    return [item.message_id for item in context[1]]


# ── Строки в базе: для тестов воркера и снимка на PostgreSQL ─────────────

NOW = T0


async def _chat(session, tg_chat_id: int, title: str = "Клиент") -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title=title, state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    return chat


async def _message(
    session,
    chat: Chat,
    tg_message_id: int,
    *,
    side: BusinessSide = BusinessSide.CLIENT,
    text: str | None = None,
    minutes: int = 0,
    media_kind: str | None = None,
    actor: TransportActorKind = TransportActorKind.HUMAN_USER,
) -> Message:
    row = Message(
        chat_id=chat.id,
        tg_message_id=tg_message_id,
        transport_actor_kind=actor,
        business_side=side,
        text=text,
        char_count=len(text or ""),
        has_media=media_kind is not None,
        media_kind=media_kind,
        sent_at=NOW + timedelta(minutes=minutes),
    )
    session.add(row)
    await session.flush()
    return row


def _row(message: Message) -> SimpleNamespace:
    """Строка очереди в том виде, в каком её отдаёт выборка `classify_pending`."""
    return SimpleNamespace(
        id=message.id, chat_id=message.chat_id, sent_at=message.sent_at
    )


async def test_fresh_same_batch_request_is_available_to_the_company():
    request = message(10, text="Подготовьте документ А")
    reply = message(11, COMPANY, text="Документ А готов")
    contexts = batch([request, reply], {request.id: (False, None, "info", None)})
    assert ids(await contexts.context(request)) == []
    contexts.record_verdict(request.id, {"label": "request", "requires_response": True})
    context = await contexts.context(reply)
    assert ids(context) == [request.id]
    assert context[1][0].first_reaction_at is None
    assert "Подготовьте документ А" in company_payload(reply.text, *context)


async def test_prior_real_answer_changes_the_next_context():
    first = message(10, text="Подготовьте документ А")
    second = message(11, text="Подготовьте документ Б")
    reply = message(12, COMPANY, text="Документ А готов")
    next_reply = message(13, COMPANY, text="Документ Б готов")
    contexts = batch([first, second, reply, next_reply], {10: REQUEST, 11: REQUEST, 12: (None, False, "ack", None)})
    assert ids(await contexts.context(reply)) == [10, 11]
    contexts.record_verdict(12, {"label": "substantive", "is_substantive": True, "answers_request_id": 10})
    assert ids(await contexts.context(next_reply)) == [11]


async def test_current_and_future_verdicts_never_change_the_prefix():
    request = message(10, seconds=0, text="Подготовьте документ А")
    reply = message(11, COMPANY, seconds=0, text="Передала специалисту")
    future = message(12, COMPANY, seconds=0, text="Документ А готов")
    later_request = message(13, seconds=0, text="Подготовьте документ Б")
    contexts = batch([request, reply, future, later_request], {
        10: REQUEST, 11: (None, False, "handoff", 10),
        12: (None, True, "substantive", 10), 13: REQUEST,
    })
    contexts.record_verdict(12, {"label": "substantive", "is_substantive": True, "answers_request_id": 10})
    tail, items = await contexts.context(reply)
    assert [event.text for event in tail] == [request.text]
    assert [item.message_id for item in items] == [10]
    assert items[0].first_reaction_at is None and items[0].handoff_at is None
    assert items[0].substantive_at is None
    _, after_handoff = await contexts.context(future)
    assert [item.message_id for item in after_handoff] == [10]
    assert after_handoff[0].handoff_at == reply.sent_at
    assert after_handoff[0].substantive_at is None


async def test_same_timestamp_uses_id_and_does_not_open_the_current_request():
    first = message(1, seconds=0, text="Подготовьте документ А")
    current = message(2, seconds=0, text="Подготовьте документ Б")
    future = message(3, seconds=0, text="Подготовьте документ В")
    contexts = batch([future, first, current], {1: REQUEST, 2: REQUEST, 3: REQUEST})
    assert ids(await contexts.context(current)) == [1]
    assert ids(await contexts.context(first)) == []


async def test_tail_keeps_media_and_filters_notices_before_limiting():
    request = message(1, text="Подготовьте документ А")
    attachment = message(2, media="document")
    notices = [message(n, COMPANY, text="Чат настроен", actor=TransportActorKind.INTEGRATOR_BOT) for n in range(3, 35)]
    target = message(35, COMPANY, text="Документ А готов")
    contexts = batch([request, attachment, *notices, target], {1: REQUEST})
    tail, _ = await contexts.context(target)
    assert len(tail) == 2
    assert tail[1].text is None and tail[1].media_kind == "document"
    assert "[документ]" in company_payload(target.text, tail, ())


async def test_future_cross_chat_broadcast_cannot_filter_a_prior_event():
    text = "Синтетическое общее уведомление компании для нескольких групп. " * 2
    blast = message(1, COMPANY, seconds=0, text=text)
    target = message(2, seconds=1, text="Подготовьте документ А")
    future = message(5, seconds=4, text="Подготовьте документ Б")
    rows = [(1, 1, blast.sent_at, text), (3, 2, T0 + timedelta(seconds=2), text), (4, 3, T0 + timedelta(seconds=3), text)]
    contexts = batch([blast, target, future], broadcasts=rows)
    assert [event.text for event in (await contexts.context(target))[0]] == [text]
    assert text not in [event.text for event in (await contexts.context(future))[0]]


async def test_forum_context_shares_the_engines_thread_boundary():
    first = message(1, text="Подготовьте документ А", thread=100)
    other = message(2, text="Подготовьте документ Б", thread=200)
    target = message(3, COMPANY, text="Документ А готов", thread=100)
    contexts = batch([first, other], {1: REQUEST, 2: REQUEST}, forum=True)
    tail, items = await contexts.context(target)
    assert [event.text for event in tail] == [first.text]
    assert [item.message_id for item in items] == [1]
    missing = SimpleNamespace(id=target.id, chat_id=1, sent_at=target.sent_at)
    with pytest.raises(ValueError, match="thread_id"):
        await contexts.context(missing)


async def test_context_limits_do_not_change_the_engine_state():
    requests = [message(n, text=f"Подготовьте документ {n}") for n in range(1, 10)]
    target = message(10, COMPANY, text="Документ готов")
    contexts = batch(requests, {m.id: REQUEST for m in requests})
    tail, items = await contexts.context(target)
    assert len(tail) == TAIL_EVENTS
    assert [item.message_id for item in items] == [7, 8, 9]
    assert len(items) == OPEN_ITEMS


@pytest.mark.parametrize("label", ["addition", "correction"])
async def test_addition_without_an_open_request_is_promoted_by_the_engine(label):
    first = message(1, text="Синтетическое уточнение документа")
    target = message(2, COMPANY, text="Проверю документ")
    contexts = batch([first], {1: (False, None, label, None)})
    assert ids(await contexts.context(target)) == [1]


async def test_prior_ack_remains_available_for_a_later_explicit_handoff():
    request = message(1, text="Подготовьте документ А")
    ack = message(2, COMPANY, text="Принято")
    target = message(3, COMPANY, text="Передала специалисту")
    contexts = batch([request, ack], {1: REQUEST, 2: (None, False, "ack", None)})
    _, items = await contexts.context(target)
    assert [item.message_id for item in items] == [1]
    assert items[0].first_reaction_at == ack.sent_at
    assert items[0].handoff_at is None


@pytest.mark.parametrize("source,visible", [("model", False), ("rule", True)])
async def test_shadow_model_overrides_obey_the_engine_contract(source, visible):
    request = message(1, text="Синтетическая просьба")
    target = message(2, COMPANY, text="Синтетический ответ")
    contexts = batch([request], {1: (False, None, "info", None)}, shadow=True)
    contexts.record_verdict(1, {"label": "request", "requires_response": True}, source=source)
    assert bool(ids(await contexts.context(target))) == visible


@pytest.mark.parametrize("company_failure", [False, True])
async def test_worker_refreshes_inputs_after_each_commit_without_a_network_transaction(monkeypatch, company_failure):
    """Цикл воркера и настоящий повтор префикса переписки при синтетическом хранении."""
    from app.config import get_settings
    from app.worker import main as worker

    request = message(1, text="Подготовьте документ А")
    reply = message(2, COMPANY, text="Документ А готов")
    later = message(3, COMPANY, text="Другие документы не нужны")
    contexts = batch([request, reply, later], {1: (False, None, "info", None)})
    pending = [SimpleNamespace(
        id=m.id, chat_id=m.chat_id, text=m.text, business_side=m.business_side,
        sent_at=m.sent_at, thread_id=m.thread_id, needs_reclassification=False,
    ) for m in [request, reply, later]]
    active = 0
    stored = []
    published = []

    class Persistence:
        async def execute(self, query):
            columns = getattr(query, "column_descriptions", ())
            return SimpleNamespace(all=lambda: [] if len(columns) == 3 else pending)

        def add(self, row):
            stored.append(row)

    @asynccontextmanager
    async def local_scope():
        nonlocal active
        active += 1
        try:
            yield Persistence()
        finally:
            active -= 1

    async def prepare(session, rows):
        assert active == 1
        assert rows == pending
        return contexts

    original_record = contexts.record_verdict

    def record(mid, verdict, source="model"):
        assert active == 0
        assert stored[-1].message_id == mid
        published.append(mid)
        original_record(mid, verdict, source)

    class Recorder:
        async def month_tokens(self, session):
            return 0

        def start_pass(self, tokens):
            pass

        async def classify_detailed(self, system, payload, **kwargs):
            assert active == 0, "provider call happened inside a transaction"
            call = len(stored)
            assert published == list(range(1, call + 1))
            expected = [] if call in (0, 2) else [request.id]
            assert kwargs["open_request_ids"] == expected
            if company_failure and call == 1:
                return {"error": "synthetic provider failure", "usage": (0, 0)}
            verdict = (
                {"label": "request", "requires_response": True}
                if call == 0 else
                {"label": "substantive", "is_substantive": True, "answers_request_id": request.id}
            )
            return {"error": None, "usage": (0, 0), "verdict": verdict}

    monkeypatch.setattr(worker, "session_scope", local_scope)
    monkeypatch.setattr(worker, "prepare_contexts", prepare)
    monkeypatch.setattr(contexts, "record_verdict", record)
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False)
    result = await worker.classify_pending(Recorder())
    if company_failure:
        assert result["classified"] == 1 and result["failed"] == 1
        assert result["deferred"] == 1
        assert [row.message_id for row in stored] == [1, 2]
        assert published == [1]
    else:
        assert result["classified"] == 3
        assert [row.message_id for row in stored] == [1, 2, 3]


@requires_db
async def test_worker_fresh_request_and_answer_share_one_database_batch(session, monkeypatch):
    from sqlalchemy import select
    from app.config import get_settings
    from app.db.models import Classification
    from app.services.tracking import open_period
    from app.worker import main as worker

    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False)
    for name in ("episode_rules_v2_since", "episode_rules_v3_since", "episode_rules_v4_since"):
        monkeypatch.setattr(get_settings(), name, NOW - timedelta(days=1))
    chat = await _chat(session, -100900070, "Синтетическая цепочка")
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))
    first = await _message(session, chat, 1, text="Подготовьте документ А", minutes=0)
    second = await _message(session, chat, 2, text="Подготовьте документ Б", minutes=0)
    reply = await _message(session, chat, 3, side=COMPANY, text="Документ А готов", minutes=0)
    await _message(session, chat, 4, side=COMPANY, text="Документ Б готов", minutes=0)
    seen = []

    @asynccontextmanager
    async def local_scope():
        yield session
        await session.flush()

    class Recorder(worker.AiClient):
        async def classify_detailed(self, system, payload, **kwargs):
            seen.append(kwargs["open_request_ids"])
            call = len(seen)
            if call <= 2:
                verdict = {"label": "request", "requires_response": True}
            else:
                verdict = {"label": "substantive", "is_substantive": True,
                           "answers_request_id": first.id if call == 3 else second.id}
            return {"error": None, "usage": (0, 0), "verdict": verdict}

    monkeypatch.setattr(worker, "session_scope", local_scope)
    result = await worker.classify_pending(Recorder())
    assert result["classified"] == 4
    assert seen == [[], [first.id], [first.id, second.id], [second.id]]
    stored = await session.scalar(select(Classification).where(Classification.message_id == reply.id))
    assert stored.answers_request_id == first.id


@requires_db
async def test_database_context_replays_the_prefix_instead_of_saved_future_state(session, monkeypatch):
    from app.config import get_settings
    from app.db.models import Classification, Interaction, InteractionState
    from app.services.classify_context import prepare_contexts
    from app.services.tracking import open_period

    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False)
    for name in ("episode_rules_v2_since", "episode_rules_v3_since", "episode_rules_v4_since"):
        monkeypatch.setattr(get_settings(), name, NOW - timedelta(days=1))
    chat = await _chat(session, -100900071, "Синтетический срез")
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))
    request = await _message(session, chat, 1, text="Подготовьте документ А", minutes=0)
    current = await _message(session, chat, 2, side=COMPANY, text="Документ А готов", minutes=0)
    future = await _message(session, chat, 3, text="Подготовьте документ Б", minutes=0)
    for mid, requires, substantive, label, link in [
        (request.id, True, None, "request", None),
        (current.id, None, True, "substantive", request.id),
        (future.id, True, None, "request", None),
    ]:
        session.add(Classification(message_id=mid, model=get_settings().ai_model,
                                   prompt_version=11, source="model", label=label,
                                   requires_response=requires, is_substantive=substantive,
                                   answers_request_id=link))
    session.add(Interaction(chat_id=chat.id, opened_at=request.sent_at,
                            opened_by_message_id=request.id, last_client_at=request.sent_at,
                            state=InteractionState.ANSWERED, first_reaction_at=current.sent_at,
                            substantive_at=current.sent_at, substantive_message_id=current.id))
    await session.flush()
    context = await (await prepare_contexts(session, [_row(current)])).context(_row(current))
    assert ids(context) == [request.id]
    assert context[1][0].first_reaction_at is None
    assert context[1][0].substantive_at is None


@requires_db
@pytest.mark.parametrize("historical_success,shadow", [(False, False), (True, False), (False, True), (True, True)])
async def test_retry_backoff_boundary_uses_accepted_success_independent_of_shadow(session, monkeypatch, historical_success, shadow):
    from sqlalchemy import select
    from app.config import get_settings
    from app.db.models import Classification
    from app.worker import main as worker
    from app.services.tracking import open_period

    monkeypatch.setattr(get_settings(), "ai_shadow_mode", shadow)
    chat = await _chat(session, -100900072, "Синтетическая ошибка")
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))
    first = await _message(session, chat, 1, side=COMPANY, text="Синтетический ответ", minutes=0)
    later = await _message(session, chat, 2, text="Синтетическая просьба", minutes=0)
    future_error = await _message(session, chat, 3, side=COMPANY, text="Будущая реплика", minutes=0)
    for target in (first, future_error):
        session.add(Classification(message_id=target.id, model=get_settings().ai_model,
                                   prompt_version=11, source="model", error="synthetic failure"))
    if historical_success:
        session.add(Classification(message_id=first.id, model=get_settings().ai_model,
                                   prompt_version=9, source="model", label="ack", is_substantive=False))
    await session.flush()
    eligible = worker.causal_pending_condition(get_settings().ai_accepted_models)
    selected = list(await session.scalars(select(Message.id).where(Message.id == later.id, eligible)))
    assert selected == ([later.id] if historical_success else [])
    # То же время с большим id — это будущее, и более раннее оно не блокирует.
    assert list(await session.scalars(select(Message.id).where(Message.id == first.id, eligible))) == [first.id]


@requires_db
async def test_unobserved_prior_error_does_not_block_the_queue(session):
    from sqlalchemy import select
    from app.config import get_settings
    from app.db.models import Classification
    from app.services.tracking import open_period
    from app.worker import main as worker

    chat = await _chat(session, -100900073, "Синтетическая пауза")
    first = await _message(session, chat, 1, side=COMPANY, text="Ошибка вне наблюдения", minutes=-1)
    await open_period(session, chat, reason="test", at=NOW)
    later = await _message(session, chat, 2, text="Наблюдаемая просьба", minutes=0)
    session.add(Classification(message_id=first.id, model=get_settings().ai_model,
                               prompt_version=11, source="model", error="synthetic failure"))
    await session.flush()
    eligible = worker.causal_pending_condition(get_settings().ai_accepted_models)
    assert list(await session.scalars(select(Message.id).where(Message.id == later.id, eligible))) == [later.id]


@requires_db
async def test_blocked_chat_larger_than_batch_does_not_starve_a_healthy_chat(session, monkeypatch):
    from sqlalchemy import select
    from app.config import get_settings
    from app.db.models import Classification
    from app.services.tracking import open_period
    from app.worker import main as worker

    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False)
    blocked = await _chat(session, -100900074, "Синтетический заблокированный чат")
    healthy = await _chat(session, -100900075, "Синтетический рабочий чат")
    for chat in (blocked, healthy):
        await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))
    error = await _message(session, blocked, 1, side=COMPANY, text="Синтетическая ошибка", minutes=0)
    session.add(Classification(message_id=error.id, model=get_settings().ai_model,
                               prompt_version=11, source="model", error="synthetic failure"))
    blocked_ids = []
    for n in range(worker.CLASSIFY_BATCH + 3):
        blocked_ids.append((await _message(session, blocked, n + 2, text="Подготовьте документ А", minutes=n + 1)).id)
    ready = await _message(session, healthy, 1, text="Подготовьте документ Б", minutes=worker.CLASSIFY_BATCH + 5)
    seen = []

    @asynccontextmanager
    async def local_scope():
        yield session
        await session.flush()

    class Recorder(worker.AiClient):
        async def classify_detailed(self, system, payload, **kwargs):
            seen.append(payload)
            return {"error": None, "usage": (0, 0),
                    "verdict": {"label": "request", "requires_response": True}}

    monkeypatch.setattr(worker, "session_scope", local_scope)
    result = await worker.classify_pending(Recorder())
    assert result["classified"] == 1 and len(seen) == 1
    assert "Подготовьте документ Б" in seen[0]
    assert await session.scalar(select(Classification.id).where(Classification.message_id == ready.id)) is not None
    assert not list(await session.scalars(select(Classification.id).where(Classification.message_id.in_(blocked_ids))))
