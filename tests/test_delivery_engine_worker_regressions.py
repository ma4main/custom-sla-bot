"""Регрессии досылки отчётов и алертов, движка эпизодов, воркера классификации,
отмены формы по /menu, обрезки названий и закрытых алертов.

Только синтетика и подставные транспорты.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape, unescape
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import (
    AiUsage,
    AlertLog,
    BotUser,
    BreachDismissal,
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
from app.services import alerts
from app.services.ai import AiClient
from app.services.ai_health import OUTCOME_FAILURE, OUTCOME_IDLE
from app.services.settings_store import set_value
from app.worker import main as worker
from tests.conftest import requires_db
from tests.test_alert_delivery import (
    GOOD,
    MANAGER_TG,
    FlakyBot,
    _handoff_overdue_episode,
    _two_recipients_and_overdue_episode,
)
from tests.test_alert_strike import RecordingBot, _case

T0 = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)


# ── Досылка отчёта отозванному адресату ──────────────────────────────────
async def test_retry_does_not_send_report_to_revoked_recipient(monkeypatch):
    from app.db import base
    from app.services import digest

    stored = SimpleNamespace(value={"retry": {"weekly": {"targets": [101], "attempts": 0}}})
    session = SimpleNamespace(get=AsyncMock(return_value=stored))

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(base, "session_scope", scope)
    monkeypatch.setattr(
        digest, "pending_retries", AsyncMock(return_value={"weekly": {"targets": [101], "attempts": 0}})
    )
    # 101 потерял доступ после первой попытки; сейчас выпуск положен только 202.
    monkeypatch.setattr(
        digest, "weekly_payload", AsyncMock(return_value=("Отчёт", [202], None, None))
    )
    sender = AsyncMock(return_value=[])
    monkeypatch.setattr(digest, "_send_issue", sender)
    await digest.retry_undelivered_issues(SimpleNamespace())
    sent_to = [target for call in sender.call_args_list for target in call.args[-1]]
    assert 101 not in sent_to, f"отозванный адресат получил отчёт: {sent_to}"


# ── Досылка алерта смотрит на текущее состояние ─────────────────────────
@requires_db
@pytest.mark.parametrize("change", ["reaction", "dismissal", "disabled"])
async def test_retry_respects_current_case_and_switches(session, change):
    await _two_recipients_and_overdue_episode(session)
    await alerts.process_alerts(session, FlakyBot())
    await session.flush()
    case = await session.scalar(select(Interaction))
    if change == "reaction":
        case.state = InteractionState.REACTED
        case.first_reaction_at = datetime.now(timezone.utc)
    elif change == "dismissal":
        session.add(BreachDismissal(chat_id=case.chat_id, opened_by_message_id=case.opened_by_message_id))
    else:
        await set_value(session, "alerts", "enabled", False, actor_id=None)
    await session.flush()
    retry = FlakyBot(failing=set())
    await alerts.retry_undelivered(session, retry)
    assert retry.attempts == [], f"неактуальный алерт дослан после: {change}"


@requires_db
async def test_retry_stops_after_specialist_contact(session):
    await _handoff_overdue_episode(session, link_manager=False)
    await alerts.process_alerts(session, FlakyBot(failing={GOOD}))
    await session.flush()
    case = await session.scalar(select(Interaction))
    case.substantive_at = datetime.now(timezone.utc)  # встречный вопрос: REACTED, но слой закрыт
    await session.flush()
    retry = FlakyBot(failing=set())
    await alerts.retry_undelivered(session, retry)
    assert retry.attempts == [], "специалист уже вышел на связь — досылать нечего"


# ── Адресаты и текст досылки ──────────────────────────────────────────────
@requires_db
async def test_retry_keeps_manager_self_recipient(session):
    await _handoff_overdue_episode(session, link_manager=True)
    await alerts.process_alerts(session, FlakyBot(failing={MANAGER_TG}))
    await session.flush()
    entry = await session.scalar(select(AlertLog))
    assert entry.delivered is False and entry.recipients == [GOOD]
    retry = FlakyBot(failing=set())
    await alerts.retry_undelivered(session, retry)
    assert retry.attempts == [MANAGER_TG], "«свой» адресат потерян при досылке"


@requires_db
async def test_retry_text_keeps_existing_reaction(session):
    await _handoff_overdue_episode(session, link_manager=False)
    await alerts.process_alerts(session, FlakyBot(failing={GOOD}))
    await session.flush()
    captured = []

    class TextBot:
        async def send_message(self, chat_id, text, **kwargs):
            captured.append(text)

    await alerts.retry_undelivered(session, TextBot())
    assert captured
    assert "Компания не отвечала" not in captured[0], "досылка забыла про реакцию менеджера"


@requires_db
async def test_personal_toggle_applies_to_self_recipient(session):
    await _handoff_overdue_episode(session, link_manager=True)
    person = await session.scalar(select(BotUser).where(BotUser.tg_user_id == MANAGER_TG))
    person.notify_personal = False
    await session.flush()
    case = await session.scalar(select(Interaction))
    assert await alerts._self_target(session, case) is None, "личка выключена, а «свой» алерт идёт"


# ── Движок эпизодов ────────────────────────────────────────────────────────
async def _chat(session, number: int) -> Chat:
    from app.services.tracking import open_period

    chat = Chat(tg_chat_id=-1009870000 - number, title="Синтетика", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=T0 - timedelta(days=1))
    return chat


async def _say(session, chat, number, side, minute, label, *, staff=None):
    from app.db.models import Attribution

    msg = Message(
        chat_id=chat.id, tg_message_id=number, business_side=side,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        text=f"сообщение {label} {number}", char_count=20,
        sent_at=T0 + timedelta(minutes=minute),
    )
    session.add(msg)
    await session.flush()
    session.add(
        Classification(
            message_id=msg.id, model=get_settings().ai_model, prompt_version=9, source="model",
            label=label,
            requires_response=(label == "request") if side is BusinessSide.CLIENT else None,
            is_substantive=False if side is BusinessSide.COMPANY else None,
        )
    )
    if staff is not None:
        session.add(Attribution(message_id=msg.id, staff_id=staff.id))
    await session.flush()
    return msg


@requires_db
async def test_only_next_client_reply_answers_the_counter_question(session, monkeypatch):
    """Ответ на встречный вопрос — одно сообщение; следующая просьба — своё обращение."""
    from app.services.episodes import rebuild_interactions

    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False)
    chat = await _chat(session, 12)
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "request")
    await _say(session, chat, 2, BusinessSide.COMPANY, 1, "question")
    await _say(session, chat, 3, BusinessSide.CLIENT, 2, "info")
    independent = await _say(session, chat, 4, BusinessSide.CLIENT, 5, "request")
    result = await rebuild_interactions(session, now=T0 + timedelta(minutes=45), persist=False)
    ours = [item for item in result["items"] if item.chat_id == chat.id]
    assert len(ours) == 2, "самостоятельная просьба после ответа клиента проглочена"
    assert ours[1].opened_by_message_id == independent.id
    assert ours[1].state is InteractionState.OPEN


@requires_db
async def test_specialist_contact_in_the_same_second_as_handoff(session, monkeypatch):
    from app.services.episodes import rebuild_interactions

    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False)
    chat = await _chat(session, 13)
    assistant = Staff(full_name="Синтетика Менеджер", normalized_name="синтетика менеджер")
    specialist = Staff(full_name="Синтетика Бухгалтер", normalized_name="синтетика бухгалтер")
    session.add_all([assistant, specialist])
    await session.flush()
    await _say(session, chat, 1, BusinessSide.CLIENT, 0, "request")
    await _say(session, chat, 2, BusinessSide.COMPANY, 1, "handoff", staff=assistant)
    contact = await _say(session, chat, 3, BusinessSide.COMPANY, 1, "ack", staff=specialist)
    result = await rebuild_interactions(session, now=T0 + timedelta(hours=2), persist=False)
    [episode] = [item for item in result["items"] if item.chat_id == chat.id]
    assert episode.substantive_at == contact.sent_at, "ответ в ту же секунду, что передача, не закрыл слой"


# ── Первое сообщение автообнаруженного чата ────────────────────────────────
@requires_db
async def test_auto_tracked_chat_keeps_its_first_message(session, monkeypatch):
    from app.services import settings_store
    from app.services.ingestion import ingest_message
    from app.services.tracking import observed_filter

    original = settings_store.get_value

    async def auto_track(session_arg, section, key):
        if (section, key) == ("chats", "auto_track_new"):
            return True
        return await original(session_arg, section, key)

    monkeypatch.setattr(settings_store, "get_value", auto_track)
    raw = {
        "message_id": 1,
        "chat": {"id": -1009870010, "type": "supergroup", "title": "Первое сообщение"},
        "from": {"id": 9870010, "is_bot": False},
        "date": int((datetime.now(timezone.utc) - timedelta(seconds=5)).timestamp()),
        "text": "Пришлите, пожалуйста, счёт",
    }
    msg = await ingest_message(session, raw)
    await session.flush()
    observed = await session.scalar(
        select(Message.id).where(Message.id == msg.id).where(observed_filter())
    )
    assert observed == msg.id, "сообщение, которым чат обнаружен, выпало из наблюдения"


# ── Воркер классификации ──────────────────────────────────────────────────
async def _pending(session, texts):
    chat = Chat(tg_chat_id=-100988500, title="Синтетика ИИ", state=ChatState.TRACKED)
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


def _use_fixture_session(monkeypatch, session):
    @asynccontextmanager
    async def local_scope():
        yield session
        await session.flush()

    monkeypatch.setattr(worker, "session_scope", local_scope)


@requires_db
@pytest.mark.parametrize(
    "texts,expected",
    [(["спасибо"], OUTCOME_IDLE), (["спасибо", "Подготовьте договор"], OUTCOME_FAILURE)],
)
async def test_rule_success_does_not_prove_provider_health(session, monkeypatch, texts, expected):
    await _pending(session, texts)
    _use_fixture_session(monkeypatch, session)

    class FailingProvider(AiClient):
        async def classify_detailed(self, *args, **kwargs):
            raise RuntimeError("провайдер лежит")

    stats = await worker.classify_pending(FailingProvider())
    assert stats["outcome"] == expected, stats


@requires_db
async def test_paid_malformed_answer_is_still_accounted(session, monkeypatch):
    await _pending(session, ["Подготовьте договор"])
    _use_fixture_session(monkeypatch, session)

    class PaidInvalidProvider(AiClient):
        async def classify_detailed(self, *args, **kwargs):
            return {"error": "битый JSON", "usage": (123, 45), "verdict": None}

    stats = await worker.classify_pending(PaidInvalidProvider())
    assert stats["failed"] == 1
    consumed = await session.scalar(
        select(func.coalesce(func.sum(AiUsage.prompt_tokens + AiUsage.completion_tokens), 0))
    )
    assert consumed == 168, "оплаченный невалидный ответ потерян в расходе"


# ── /menu отменяет форму ──────────────────────────────────────────────────
async def test_menu_cancels_pending_staff_form(monkeypatch):
    from aiogram import Bot, Dispatcher, Router
    from aiogram.filters import Command
    from aiogram.types import Update

    from app.bot.handlers import start
    from app.bot.handlers.staff_ui import StaffForm
    from app.db.models import BotRole, BotUserState

    user = BotUser(
        id=9181, tg_user_id=9181, display_name="Владелец",
        role=BotRole.OWNER, state=BotUserState.ACTIVE, permissions={},
    )
    session = SimpleNamespace(expunge=lambda obj: None)

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(start, "session_scope", scope)
    monkeypatch.setattr(start, "get_user", AsyncMock(return_value=user))
    monkeypatch.setattr(start, "register_start", AsyncMock(return_value=user))
    dispatcher = Dispatcher()
    router = Router()
    router.message.register(start.on_start, Command("menu"))
    dispatcher.include_router(router)
    bot = Bot(token="123456:synthetic_test_token")
    monkeypatch.setattr(bot, "session", AsyncMock(return_value=True))
    state = dispatcher.fsm.get_context(bot=bot, chat_id=9181, user_id=9181)
    await state.set_state(StaffForm.waiting_alias)
    await state.update_data(staff_id=991)
    update = Update.model_validate(
        {
            "update_id": 9181,
            "message": {
                "message_id": 9181, "date": 1788249600,
                "chat": {"id": 9181, "type": "private"},
                "from": {"id": 9181, "is_bot": False, "first_name": "Владелец"},
                "text": "/menu",
                "entities": [{"offset": 0, "length": 5, "type": "bot_command"}],
            },
        }
    )
    await dispatcher.feed_update(bot, update)
    assert await state.get_state() is None, "после /menu обычный текст ушёл бы в брошенную форму"


# ── Обрезка названий ──────────────────────────────────────────────────────
def _patch_summary(monkeypatch, title):
    from app.bot.handlers import reports
    from app.services import calendar, report_lab

    summary = dict(
        tracked=1, incoming=10, outgoing=10, unresolved=0, unresolved_pct=0,
        chats=[dict(title=title, incoming=5, outgoing=5)], staff=[],
    )
    speed = dict(
        total=1, answered=1, waiting=0, waiting_paused=0, timed_out=0, no_response=0,
        wait_reaction_hours=24, wait_specialist_days=7, reaction_limit_min=30,
        ttfr_median=60, ttfr_p90=60, breach_reaction=0, substantive_limit_min=1440,
        ttfa_median=60, ttfa_p90=60, breach_substantive=0, handoffs=0,
    )
    monkeypatch.setattr(reports, "load_summary", AsyncMock(return_value=summary))
    monkeypatch.setattr(reports, "load_speed", AsyncMock(return_value=speed))
    monkeypatch.setattr(reports, "get_section", AsyncMock(return_value={}))
    monkeypatch.setattr(report_lab, "staff_breaches", AsyncMock(return_value={}))
    monkeypatch.setattr(
        calendar, "CalendarHistory", lambda cfg: SimpleNamespace(footnote=lambda *args: None)
    )
    return reports


async def test_chat_title_is_cut_before_escaping(monkeypatch):
    title = "x" * 38 + "&Z"
    reports = _patch_summary(monkeypatch, title)
    now = datetime.now(timezone.utc)
    body = await reports._render_all(None, now, now)
    assert "&amp;" not in unescape(re.sub(r"<[^>]+>", "", body)) or escape(title) in body
    assert escape(title[:40]) in body, "обрезка порвала сущность в названии"


# ── Открытые алерты не заслоняют закрытый ─────────────────────────────────
@requires_db
async def test_twenty_open_alerts_do_not_starve_a_closed_one(session):
    now = datetime.now(timezone.utc)
    for n in range(20):
        await _case(
            session, -100988000 - n, f"Ждёт {n}",
            alert_sent_at=now - timedelta(minutes=40),
            opened_at=now - timedelta(hours=2), state=InteractionState.OPEN,
        )
    await _case(
        session, -100988099, "Закрыт после двадцати ждущих",
        alert_sent_at=now - timedelta(minutes=35),
        opened_at=now - timedelta(hours=2), state=InteractionState.ANSWERED,
        first_reaction_at=now - timedelta(minutes=10), ttfr_business_seconds=6600,
    )
    bot = RecordingBot()
    await alerts.strike_closed_alerts(session, bot)
    assert len(bot.edits) == 1, "лимит применён раньше фильтра по исходу — закрытый не зачёркнут"
