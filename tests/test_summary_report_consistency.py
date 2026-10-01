"""Сводный отчёт укладывается в лимит Telegram; личные и сводные показатели
считаются одинаково."""

from datetime import datetime, timedelta, timezone
from html import unescape
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    Staff,
    TransportActorKind,
)
from tests.conftest import requires_db


def _patch_summary(monkeypatch, *, populated=True, title=None):
    from app.bot.handlers import reports
    from app.services import calendar, report_lab

    chats = [
        dict(title=title or ("Клиент " + str(i) + " " + "x" * 30), incoming=50, outgoing=50)
        for i in range(8 if populated else 1)
    ]
    summary = dict(
        tracked=150, incoming=1000, outgoing=1000, unresolved=0, unresolved_pct=0, chats=chats,
        staff=[
            dict(id=i, full_name="Сотрудник " + str(i) + " " + "x" * 30, messages=100,
                 chats_touched=10, active_days=7)
            for i in range(8)
        ] if populated else [],
    )
    speed = dict(
        total=100, answered=50, waiting=30, waiting_paused=0, timed_out=20, no_response=0,
        wait_reaction_hours=24, wait_specialist_days=7, reaction_limit_min=30, ttfr_median=600,
        ttfr_p90=3600, breach_reaction=30, substantive_limit_min=1440, ttfa_median=3600,
        ttfa_p90=7200, breach_substantive=5, handoffs=10,
    )
    breaches = {
        i: dict(count=7, handled=10, share=70, reaction=4, specialist=3,
                chats={("Клиент " + str(j) + " " + "x" * 30): 1 for j in range(6)})
        for i in range(8)
    }
    monkeypatch.setattr(reports, "load_summary", AsyncMock(return_value=summary))
    monkeypatch.setattr(reports, "load_speed", AsyncMock(return_value=speed))
    monkeypatch.setattr(reports, "get_section", AsyncMock(return_value={}))
    monkeypatch.setattr(report_lab, "staff_breaches", AsyncMock(return_value=breaches))
    monkeypatch.setattr(
        calendar, "CalendarHistory", lambda cfg: SimpleNamespace(footnote=lambda *args: None)
    )
    return reports


async def test_full_summary_fits_telegram_and_points_to_html(monkeypatch):
    """Длинная сводка ужимает списки до лимита Telegram и ссылается на HTML-файл."""
    reports = _patch_summary(monkeypatch)
    now = datetime.now(timezone.utc)
    body = await reports._render_all(None, now, now)
    visible = unescape(re.sub(r"<[^>]+>", "", body))
    assert len(visible) <= reports.SUMMARY_BUDGET, f"тело не влезает: {len(visible)}"
    assert reports.HTML_NOTE in body, "списки ужаты, а пометки про HTML-файл нет"
    assert "Скорость (рабочее время)" in body, "цифры не режутся, режутся только списки"


async def test_small_summary_has_no_html_note(monkeypatch):
    reports = _patch_summary(monkeypatch, populated=False)
    now = datetime.now(timezone.utc)
    body = await reports._render_all(None, now, now)
    assert reports.HTML_NOTE not in body, "ничего не урезано — пометка лишняя"


@requires_db
async def test_personal_percentiles_match_summary_on_the_same_population(session):
    """Личные медиана и p90 считаются так же, как сводные."""
    from app.services.report_data import load_speed, load_staff_report
    from app.services.tracking import open_period

    start = datetime(2026, 8, 24, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    chat = Chat(tg_chat_id=-10081818281, title="Перцентили", state=ChatState.TRACKED)
    person = Staff(full_name="Синтетика Менеджер", normalized_name="синтетика менеджер")
    session.add_all([chat, person])
    await session.flush()
    await open_period(session, chat, reason="test", at=start)
    for index, seconds in enumerate([60, 180], 1):
        opened = start + timedelta(hours=index)
        message = Message(
            chat_id=chat.id, tg_message_id=index,
            transport_actor_kind=TransportActorKind.HUMAN_USER, business_side=BusinessSide.CLIENT,
            text="Вопрос", char_count=6, sent_at=opened,
        )
        session.add(message)
        await session.flush()
        session.add(
            Interaction(
                chat_id=chat.id, opened_at=opened, opened_by_message_id=message.id,
                last_client_at=opened, client_messages=1, state=InteractionState.ANSWERED,
                first_reaction_staff_id=person.id, ttfr_business_seconds=seconds,
            )
        )
    await session.flush()
    personal = await load_staff_report(session, person.id, start, end)
    summary = await load_speed(session, start, end)
    assert personal["reaction_median"] == summary["ttfr_median"] == 120
    assert personal["reaction_p90"] == summary["ttfr_p90"]


@requires_db
async def test_specialist_personal_report_counts_late_substantive_reply(session):
    """Личный отчёт специалиста видит просрочку второй ступени."""
    from app.services.report_data import load_staff_report
    from app.services.report_lab import staff_breaches
    from app.services.tracking import open_period

    start = datetime(2026, 8, 24, tzinfo=timezone.utc)
    end = start + timedelta(days=3)
    chat = Chat(tg_chat_id=-10081818282, title="Специалист", state=ChatState.TRACKED)
    person = Staff(full_name="Синтетика Специалист", normalized_name="синтетика специалист")
    session.add_all([chat, person])
    await session.flush()
    await open_period(session, chat, reason="test", at=start)
    message = Message(
        chat_id=chat.id, tg_message_id=1,
        transport_actor_kind=TransportActorKind.HUMAN_USER, business_side=BusinessSide.CLIENT,
        text="Вопрос", char_count=6, sent_at=start,
    )
    session.add(message)
    await session.flush()
    session.add(
        Interaction(
            chat_id=chat.id, opened_at=start, opened_by_message_id=message.id,
            last_client_at=start, client_messages=1, state=InteractionState.ANSWERED,
            substantive_staff_id=person.id, handoff_at=start + timedelta(minutes=10),
            substantive_at=start + timedelta(days=2), substantive_breached=True, sla_breached=False,
        )
    )
    await session.flush()
    personal = await load_staff_report(session, person.id, start, end)
    summary_staff = await staff_breaches(session, start, end)
    assert personal["breached"] == summary_staff[person.id]["count"] == 1
