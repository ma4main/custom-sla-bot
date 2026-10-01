"""Регрессии по отдельным исправлениям: имя каждого теста говорит,
какое поведение проверяется."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    BotRole,
    BotUser,
    BotUserState,
    BusinessSide,
    Chat,
    ChatState,
    ChatTrackingPeriod,
    Message,
    Staff,
    TelegramUpdate,
    TransportActorKind,
)
from tests.conftest import requires_db

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


# ── нейтральный ответ ──────────────────────────────────────────────────
def test_neutral_reply_reveals_nothing_and_is_shared():
    from app.bot import middlewares
    from app.bot.handlers import fallback, start
    from app.text import NEUTRAL_REPLY

    lowered = NEUTRAL_REPLY.lower()
    for word in ("заявк", "администратор", "роль", "доступ", "одобр", "рассмотр"):
        assert word not in lowered, f"ответ незнакомцу раскрывает систему доступа: «{word}»"
    assert start.NEUTRAL_REPLY is NEUTRAL_REPLY
    assert middlewares.NEUTRAL_REPLY is NEUTRAL_REPLY
    assert fallback.NEUTRAL_REPLY is NEUTRAL_REPLY, "копия константы разошлась бы при правке"


# ── сотрудник напрямую ──────────────────────────────────────────────────
@requires_db
async def test_linked_bot_user_is_company_side(session):
    from app.services.ingestion import resolve_business_side

    person = Staff(full_name="Ирина Соколова", normalized_name="ирина соколова")
    session.add(person)
    await session.flush()
    session.add(
        BotUser(
            tg_user_id=880001,
            role=BotRole.MANAGER,
            permissions={},
            state=BotUserState.ACTIVE,
            staff_id=person.id,
        )
    )
    await session.flush()

    side = await resolve_business_side(session, TransportActorKind.HUMAN_USER, 880001, "текст")
    assert side is BusinessSide.COMPANY, "привязанный сотрудник считался клиентом"
    stranger = await resolve_business_side(session, TransportActorKind.HUMAN_USER, 880002, "т")
    assert stranger is BusinessSide.CLIENT


# ── правка в чате на паузе ──────────────────────────────────────────────
@requires_db
async def test_edited_message_in_paused_chat_stays_in_queue(session):
    from app.services.ai_stats import pending_conditions

    paused = Chat(tg_chat_id=-100970001, title="Пауза", state=ChatState.PAUSED)
    session.add(paused)
    await session.flush()
    session.add_all(
        [
            Message(
                chat_id=paused.id, tg_message_id=1, tg_user_id=1,
                transport_actor_kind=TransportActorKind.HUMAN_USER,
                business_side=BusinessSide.CLIENT, text="старое", char_count=6,
                sent_at=NOW, needs_reclassification=False,
            ),
            Message(
                chat_id=paused.id, tg_message_id=2, tg_user_id=1,
                transport_actor_kind=TransportActorKind.HUMAN_USER,
                business_side=BusinessSide.CLIENT, text="правка", char_count=6,
                sent_at=NOW, needs_reclassification=True,
            ),
        ]
    )
    await session.flush()

    ids = (
        await session.scalars(
            select(Message.id).where(*pending_conditions("vendor/previous-model"))
            .where(Message.chat_id == paused.id)
        )
    ).all()
    texts = {(await session.get(Message, i)).text for i in ids}
    assert texts == {"правка"}, "правка в паузном чате обязана дойти до классификатора"


# ── окно аварии в журнале ───────────────────────────────────────────────
@requires_db
async def test_replay_picks_up_stale_unprocessed_updates(session):
    from app.services.reprocess import replay_failed_updates

    session.add(
        TelegramUpdate(
            update_id=9900001,
            update_type="message",
            payload={"update_id": 9900001},  # без тела — переигровка честно упадёт
            received_at=NOW - timedelta(hours=2),
            processed_at=None,
            processing_error=None,
        )
    )
    session.add(
        TelegramUpdate(
            update_id=9900002,
            update_type="message",
            payload={"update_id": 9900002},
            received_at=datetime.now(timezone.utc),  # свежий — его сейчас обрабатывает бот
            processed_at=None,
            processing_error=None,
        )
    )
    await session.flush()

    result = await replay_failed_updates(session)
    assert result["found"] == 1, "старый необработанный апдейт без ошибки не переигрывался"
    stale = await session.get(TelegramUpdate, 9900001)
    assert stale.processing_error, "попытка обязана оставить след"
    fresh = await session.get(TelegramUpdate, 9900002)
    assert fresh.processing_error is None, "свежий апдейт трогать нельзя"


# ── жёсткий потолок токенов ─────────────────────────────────────────────
def test_budget_reserves_the_upcoming_call(monkeypatch):
    from app.services import ai

    class _S:
        ai_base_url = "http://x"
        ai_model = "m"
        ai_api_key = "k"
        ai_monthly_token_limit = 1000

    monkeypatch.setattr(ai, "get_settings", lambda: _S())
    client = ai.AiClient()
    client.start_pass(900)
    client.check_budget()  # потрачено < потолка
    with pytest.raises(ai.AiBudgetExceeded):
        client.check_budget(reserve=500)  # а запрос пробил бы потолок


# ── content=null ────────────────────────────────────────────────────────
def test_null_content_is_a_model_error_not_a_typeerror():
    from app.services.ai import _parse_json

    for bad in (None, "", "   "):
        with pytest.raises(RuntimeError):
            _parse_json(bad)


# ── ретеншн догоняет долг ───────────────────────────────────────────────
@requires_db
async def test_retention_drains_beyond_one_batch(session, monkeypatch):
    from app.services import retention
    from app.services.settings_store import set_value

    monkeypatch.setattr(retention, "BATCH", 2)
    await set_value(session, "retention", "raw_update_days", 1, actor_id=None)
    old = datetime.now(timezone.utc) - timedelta(days=5)
    session.add_all(
        [
            TelegramUpdate(update_id=9910000 + n, update_type="message", payload={}, received_at=old)
            for n in range(5)
        ]
    )
    await session.flush()

    result = await retention.apply_retention(session)
    assert result["removed_updates"] == 5, "одна пачка в сутки оставляла долг"


# ── служебная группа вне отчётов ────────────────────────────────────────
@requires_db
async def test_reports_ignore_historical_notify_group(session, monkeypatch):
    from app import config
    from app.services.tracking import observed_filter, open_period

    monkeypatch.setattr(config.get_settings(), "notify_group_chat_id", -100555, raising=False)
    notify = Chat(tg_chat_id=-100555, title="Уведомления", state=ChatState.ARCHIVED)
    client = Chat(tg_chat_id=-100556, title="Клиент", state=ChatState.TRACKED)
    session.add_all([notify, client])
    await session.flush()
    for chat in (notify, client):
        await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))
        session.add(
            Message(
                chat_id=chat.id, tg_message_id=1, tg_user_id=5,
                transport_actor_kind=TransportActorKind.HUMAN_USER,
                business_side=BusinessSide.CLIENT, text="x", char_count=1, sent_at=NOW,
            )
        )
    await session.flush()

    observed = await session.scalar(
        select(func.count(Message.id)).where(observed_filter())
        .where(Message.chat_id.in_([notify.id, client.id]))
    )
    assert observed == 1, "история служебной группы попадала в отчёты"


# ── один открытый интервал на чат ───────────────────────────────────────
@requires_db
async def test_only_one_open_tracking_period_per_chat(session):
    from app.services.tracking import open_period

    chat = Chat(tg_chat_id=-100970009, title="Гонка", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="a")
    await open_period(session, chat, reason="b")
    opened = await session.scalar(
        select(func.count(ChatTrackingPeriod.id))
        .where(ChatTrackingPeriod.chat_id == chat.id)
        .where(ChatTrackingPeriod.ended_at.is_(None))
    )
    assert opened == 1

    # Прямая вставка второго открытого интервала упирается в индекс базы,
    # а не в проверку в коде.
    session.add(ChatTrackingPeriod(chat_id=chat.id, started_at=NOW, reason="dup"))
    with pytest.raises(IntegrityError):
        await session.flush()
