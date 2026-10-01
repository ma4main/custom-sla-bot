"""Сторона и автор при привязке сотрудника и правке подписи; воркер: переразметка
правок, стартовая отсрочка сторожа, пошаговое сохранение вердиктов."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db.models import (
    Attribution,
    BotRole,
    BotUser,
    BotUserState,
    BusinessSide,
    Chat,
    ChatState,
    Classification,
    Message,
    Staff,
    TransportActorKind,
)
from app.services.ai import AiClient
from app.worker import main as worker
from tests.conftest import requires_db

T0 = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)


async def _chat(session, number: int) -> Chat:
    from app.services.tracking import open_period

    chat = Chat(tg_chat_id=-1009870100 - number, title="Синтетика", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=T0 - timedelta(days=1))
    return chat


# ── Привязка сотрудника ───────────────────────────────────────────────────
@requires_db
async def test_linked_direct_staff_gets_attribution(session):
    from app.services.attribution import attribute_message
    from app.services.ingestion import resolve_business_side

    chat = await _chat(session, 1)
    person = Staff(full_name="Синтетика Специалист", normalized_name="синтетика специалист")
    session.add(person)
    await session.flush()
    session.add(
        BotUser(
            tg_user_id=9870011, role=BotRole.MANAGER, permissions={},
            state=BotUserState.ACTIVE, staff_id=person.id,
        )
    )
    await session.flush()
    side = await resolve_business_side(session, TransportActorKind.HUMAN_USER, 9870011, "Принято")
    assert side is BusinessSide.COMPANY
    msg = Message(
        chat_id=chat.id, tg_message_id=1, business_side=side,
        transport_actor_kind=TransportActorKind.HUMAN_USER, tg_user_id=9870011,
        text="Принято", char_count=7, sent_at=T0,
    )
    session.add(msg)
    await session.flush()
    await attribute_message(session, msg)
    await session.flush()
    attribution = await session.get(Attribution, msg.id)
    assert attribution is not None and attribution.staff_id == person.id, (
        "привязанный сотрудник — сторона компании без автора"
    )


@requires_db
async def test_link_staff_invalidates_client_verdicts(session):
    from app.services.access import link_staff

    chat = await _chat(session, 2)
    person = Staff(full_name="Синтетика Привязанный", normalized_name="синтетика привязанный")
    actor = BotUser(tg_user_id=98700151, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)
    target = BotUser(tg_user_id=98700152, role=BotRole.MANAGER, permissions={}, state=BotUserState.ACTIVE)
    session.add_all([person, actor, target])
    await session.flush()
    msg = Message(
        chat_id=chat.id, tg_message_id=1, business_side=BusinessSide.CLIENT,
        transport_actor_kind=TransportActorKind.HUMAN_USER, tg_user_id=target.tg_user_id,
        text="Пришлите счёт", char_count=13, sent_at=T0,
    )
    session.add(msg)
    await session.flush()
    session.add(
        Classification(
            message_id=msg.id, model=get_settings().ai_model, prompt_version=9,
            source="model", label="request", requires_response=True,
        )
    )
    await session.flush()

    await link_staff(session, actor, target, person.id)
    await session.flush()
    await session.refresh(msg)
    assert msg.business_side is BusinessSide.COMPANY
    assert msg.needs_reclassification is True
    stale = await session.scalar(select(Classification).where(Classification.message_id == msg.id))
    assert stale is None, "клиентский вердикт остался действовать на стороне компании"


# ── Правка подписи ────────────────────────────────────────────────────────
@requires_db
async def test_edit_with_client_mark_flips_side(session, monkeypatch):
    from app.services.ingestion import ingest_message

    monkeypatch.setattr(get_settings(), "integrator_bot_id", 9870014)
    chat = await _chat(session, 3)
    raw = {
        "message_id": 1,
        "chat": {"id": chat.tg_chat_id, "type": "supergroup", "title": chat.title},
        "from": {"id": 9870014, "is_bot": True},
        "date": int(T0.timestamp()),
        "text": "Синтетика Человек [corp.example] пишет:\nПришлите счёт",
    }
    original = await ingest_message(session, raw)
    assert original.business_side is BusinessSide.COMPANY
    assert await session.get(Attribution, original.id) is not None
    edited = {
        **raw,
        "edit_date": int((T0 + timedelta(minutes=1)).timestamp()),
        "text": "(К) Синтетика Человек [corp.example] пишет:\nПришлите счёт",
    }
    msg = await ingest_message(session, edited, is_edit=True)
    await session.flush()
    assert msg.business_side is BusinessSide.CLIENT, "правка подписи не сменила сторону"
    assert msg.needs_reclassification is True
    assert await session.get(Attribution, msg.id) is None, "клиент остался «нераспознанным автором»"


def _use_fixture_session(monkeypatch, session):
    @asynccontextmanager
    async def local_scope():
        yield session
        await session.flush()

    monkeypatch.setattr(worker, "session_scope", local_scope)


@requires_db
async def test_captionless_edits_do_not_starve_later_edits(session, monkeypatch):
    from app.services.ai import PROMPT_VERSION

    chat = await _chat(session, 4)
    messages = []
    for n, text in enumerate(["подпись стёрта", "подпись стёрта 2", "Новая срочная просьба"]):
        message = Message(
            chat_id=chat.id, tg_message_id=n + 1, business_side=BusinessSide.CLIENT,
            transport_actor_kind=TransportActorKind.HUMAN_USER, text=text, char_count=len(text),
            sent_at=T0 + timedelta(seconds=n), needs_reclassification=True,
        )
        session.add(message)
        messages.append(message)
    await session.flush()
    for message in messages[:2]:
        message.text = None
        message.has_media = True
    last = messages[-1]
    session.add(
        Classification(
            message_id=last.id, model=get_settings().ai_model, prompt_version=PROMPT_VERSION,
            label="ack", requires_response=False, source="rule",
        )
    )
    await session.flush()
    _use_fixture_session(monkeypatch, session)
    # Пачка из двух воспроизводит границу с тремя записями вместо 51.
    for _ in range(3):
        await worker.reprocess_edited(batch=2)
    stale = await session.scalar(select(Classification).where(Classification.message_id == last.id))
    assert stale is None, "правки без текста держат флаг и заслоняют хвост очереди"
    for message in messages[:2]:
        await session.refresh(message)
        assert message.needs_reclassification is False


# ── Воркер ────────────────────────────────────────────────────────────────
def test_restart_grace_ignores_previous_process_heartbeat():
    from app.health import watchdog_reason

    assert watchdog_reason(age=400, uptime=1, limit=300) is None, (
        "отметка прошлого процесса отменяет стартовую отсрочку"
    )
    assert watchdog_reason(age=400, uptime=1000, limit=300) is not None
    assert watchdog_reason(age=10, uptime=1000, limit=300) is None


async def _pending(session, texts):
    chat = Chat(tg_chat_id=-100988600, title="Синтетика ИИ", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    for n, value in enumerate(texts):
        session.add(
            Message(
                chat_id=chat.id, tg_message_id=n + 1,
                transport_actor_kind=TransportActorKind.HUMAN_USER,
                business_side=BusinessSide.CLIENT, text=value, char_count=len(value),
                sent_at=datetime.now(timezone.utc) + timedelta(seconds=n),
            )
        )
    await session.flush()


@requires_db
async def test_results_are_persisted_one_by_one(session, monkeypatch):
    """Второй запрос уронил процесс — вердикт первого уже в базе."""
    await _pending(session, ["Подготовьте договор", "Сделайте счёт"])
    _use_fixture_session(monkeypatch, session)
    calls = {"n": 0}

    class DiesOnSecond(AiClient):
        async def classify_detailed(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise SystemExit("сторож убил воркер")
            return {"error": None, "usage": (10, 2), "verdict": {"label": "request", "requires_response": True}}

    try:
        await worker.classify_pending(DiesOnSecond())
    except SystemExit:
        pass
    stored = (await session.scalars(select(Classification))).all()
    assert len(stored) == 1 and stored[0].label == "request", "результат первого запроса потерян"


@requires_db
async def test_pass_stops_at_deadline(session, monkeypatch):
    await _pending(session, ["Подготовьте договор", "Сделайте счёт", "Ещё вопрос"])
    _use_fixture_session(monkeypatch, session)
    monkeypatch.setattr(worker, "CLASSIFY_DEADLINE_SECONDS", 0)

    class Slow(AiClient):
        async def classify_detailed(self, *args, **kwargs):
            return {"error": None, "usage": (1, 1), "verdict": {"label": "request", "requires_response": True}}

    stats = await worker.classify_pending(Slow())
    assert stats["classified"] == 1, stats
    assert stats["deadline_hit"] is True


@requires_db
async def test_context_failure_is_recorded_and_does_not_break_the_pass(session, monkeypatch):
    """Сбой сборки контекста одного сообщения — отказ этого сообщения, а не обрыв прохода
    и не «провайдер недоступен»."""
    await _pending(session, ["Подготовьте договор"])
    _use_fixture_session(monkeypatch, session)

    class BrokenContext:
        async def context(self, row):
            raise RuntimeError("битый снимок")

        def record_verdict(self, *args):
            raise AssertionError("вердикта быть не могло")

    async def prepare(session, pending):
        return BrokenContext()

    monkeypatch.setattr(worker, "prepare_contexts", prepare)

    class NotCalled(AiClient):
        async def classify_detailed(self, *args, **kwargs):
            raise AssertionError("провайдер без входа звать нельзя")

    stats = await worker.classify_pending(NotCalled())
    assert stats["attempted"] == 0 and stats["failed"] == 0, stats
    stored = (await session.scalars(select(Classification))).all()
    assert len(stored) == 1 and "RuntimeError" in (stored[0].error or ""), (
        "отказ сборки контекста не записан попыткой сообщения"
    )


@requires_db
async def test_payload_failure_before_the_call_is_an_ordinary_refusal(session, monkeypatch):
    """Отказ до сетевого вызова: длительности нет, а не UnboundLocalError на весь проход."""
    await _pending(session, ["Подготовьте договор"])
    _use_fixture_session(monkeypatch, session)

    def broken_text(*args, **kwargs):
        raise ValueError("битое медиа")

    monkeypatch.setattr(worker, "current_message_text", broken_text)

    stats = await worker.classify_pending(AiClient())
    assert stats["failed"] == 1 and stats["latencies_ms"] == [], stats
    stored = (await session.scalars(select(Classification))).all()
    assert len(stored) == 1 and "ValueError" in (stored[0].error or "")


async def test_daily_task_failure_does_not_stop_alerts(monkeypatch):
    """Упавшая суточная задача: соседние задачи, пульс и алерты идут, повтор — через сутки."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services import digest, uptime

    @asynccontextmanager
    async def scope():
        yield SimpleNamespace()

    async def broken(session):
        raise RuntimeError("битая строка")

    beats: list[str] = []
    replay = AsyncMock()
    roles = AsyncMock()
    process = AsyncMock()
    monkeypatch.setattr(worker, "session_scope", scope)
    monkeypatch.setattr(get_settings(), "ai_enabled", False)
    monkeypatch.setattr(worker, "_last_daily_at", None)
    monkeypatch.setattr(worker, "reprocess_edited", AsyncMock(return_value=0))
    monkeypatch.setattr(worker, "rebuild_interactions", AsyncMock())
    monkeypatch.setattr(worker, "apply_retention", broken)
    monkeypatch.setattr(worker, "replay_failed_updates", replay)
    monkeypatch.setattr(worker, "refresh_observed_roles", roles)
    monkeypatch.setattr(worker, "beat", beats.append)
    monkeypatch.setattr(worker, "alerts_paused", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(worker, "retry_undelivered", AsyncMock(return_value=0))
    monkeypatch.setattr(worker, "process_alerts", process)
    monkeypatch.setattr(worker, "strike_closed_alerts", AsyncMock(return_value=0))
    for name in (
        "maybe_send_evening_digest",
        "maybe_send_weekly_report",
        "maybe_send_monthly_report",
        "maybe_send_alert_digest",
        "maybe_send_link_nudge",
    ):
        monkeypatch.setattr(worker, name, AsyncMock(return_value=False))
    monkeypatch.setattr(digest, "retry_undelivered_issues", AsyncMock(return_value=0))
    monkeypatch.setattr(uptime, "record_beat", AsyncMock())

    await worker.run_once(bot=SimpleNamespace())
    assert beats == ["worker"], "пульс не отмечен после сбоя суточной задачи"
    assert process.await_count == 1, "алерты не ушли после сбоя суточной задачи"
    replay.assert_awaited_once()
    roles.assert_awaited_once()

    await worker.run_once(bot=SimpleNamespace())
    assert process.await_count == 2
    # Повтор — через сутки, а не следующим тиком.
    replay.assert_awaited_once()


@requires_db
async def test_link_staff_keeps_human_client_decision(session):
    """Человек решил «это клиент» — привязка учётки к сотруднику решение не перебивает."""
    from app.db.models import SenderRule, SenderRuleKind, SenderRuleSide
    from app.services.access import link_staff

    chat = await _chat(session, 5)
    person = Staff(full_name="Синтетика Решённый", normalized_name="синтетика решённый")
    actor = BotUser(tg_user_id=98700161, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)
    target = BotUser(tg_user_id=98700162, role=BotRole.MANAGER, permissions={}, state=BotUserState.ACTIVE)
    session.add_all([person, actor, target])
    await session.flush()
    session.add(
        SenderRule(
            kind=SenderRuleKind.TG_USER, key=target.tg_user_id, chat_id=None,
            side=SenderRuleSide.CLIENT, decided_by=actor.id,
        )
    )
    msg = Message(
        chat_id=chat.id, tg_message_id=1, business_side=BusinessSide.CLIENT,
        transport_actor_kind=TransportActorKind.HUMAN_USER, tg_user_id=target.tg_user_id,
        text="Пришлите счёт", char_count=13, sent_at=T0,
    )
    session.add(msg)
    await session.flush()
    session.add(
        Classification(
            message_id=msg.id, model=get_settings().ai_model, prompt_version=9,
            source="model", label="request", requires_response=True,
        )
    )
    await session.flush()

    await link_staff(session, actor, target, person.id)
    await session.flush()
    await session.refresh(msg)
    assert msg.business_side is BusinessSide.CLIENT, "привязка перебила решение человека"
    assert msg.needs_reclassification is False
    kept = await session.scalar(select(Classification).where(Classification.message_id == msg.id))
    assert kept is not None, "вердикт клиента стёрт, хотя сторона не менялась"
