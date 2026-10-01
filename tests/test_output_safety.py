"""Безопасность вывода: чужой текст не должен ломать интерфейс и таблицы."""

import pytest

from app.services.export import _defuse
from app.text import esc


@pytest.mark.parametrize(
    "dangerous",
    ['=HYPERLINK("http://evil","жми")', "+CMD|calc", "-2+3", "@SUM(A1)", "\tтаб"],
)
def test_formula_like_values_are_defused(dangerous):
    """Значения, похожие на формулу, обезвреживаются: название группы задают её участники."""
    assert _defuse(dangerous).startswith("'")


@pytest.mark.parametrize(
    "dangerous",
    [
        ' =HYPERLINK("http://evil","жми")',
        "   +CMD|calc",
        "\n=cmd",
        "\r\n@SUM(A1)",
        "\t =1+1",
    ],
)
def test_formula_with_leading_whitespace_is_defused(dangerous):
    """Часть импортёров обрезает ведущие пробелы и переводы строк, и за ними
    тоже может стоять формула.
    """
    assert _defuse(dangerous).startswith("'")


@pytest.mark.parametrize("safe", ["ООО «Ромашка»", "Бухгалтерия: ИП Смирнов", ""])
def test_safe_values_are_untouched(safe):
    assert _defuse(safe) == safe


def test_numbers_stay_numbers():
    assert _defuse(42) == 42
    assert _defuse(None) is None


def test_html_special_chars_are_escaped():
    assert esc('ООО "Ромашка" & <партнёры>') == 'ООО "Ромашка" &amp; &lt;партнёры&gt;'


def test_escape_handles_none_and_numbers():
    assert esc(None) == ""
    assert esc(17) == "17"


# ── Обрезка и экранирование: сначала обрезать, потом экранировать ─────────
_CUT_TITLE = "x" * 37 + "&Z"


def _no_torn_entity(html: str) -> bool:
    """Каждый «&» — начало целой сущности."""
    import re

    return all(
        re.match(r"&(amp|lt|gt|quot|#\d+);", html[pos:]) for pos in range(len(html))
        if html[pos] == "&"
    )


def test_attention_title_is_cut_before_escaping():
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from app.bot.handlers.reports_lab import _render_attention

    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    item = dict(
        title=_CUT_TITLE, overdue=True, reacted=False,
        opened_at=now - timedelta(hours=2), business_age=3600,
    )
    text = _render_attention([item], ZoneInfo("Europe/Moscow"), now)
    assert _no_torn_entity(text), "обрезка порвала сущность в названии чата"
    assert esc(_CUT_TITLE[:38]) in text


def test_breach_details_title_is_cut_before_escaping():
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from app.services.html_report import _breach_details

    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    item = dict(
        title="x" * 39 + "&Z", kind="reaction", opened_at=now, since=now,
        until=now + timedelta(hours=1), business_delay=3600,
    )
    html = _breach_details(
        [dict(staff_id=1, full_name="Синтетика")],
        {1: dict(count=1, items=[item])},
        ZoneInfo("Europe/Moscow"),
        {},
    )
    assert _no_torn_entity(html), "обрезка порвала сущность в названии чата"


async def test_new_staff_name_is_escaped_in_confirmation(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.bot.handlers import staff_ui

    @asynccontextmanager
    async def scope():
        yield SimpleNamespace()

    monkeypatch.setattr(staff_ui, "session_scope", scope)
    monkeypatch.setattr(
        staff_ui, "create_staff", AsyncMock(return_value=SimpleNamespace(full_name="Иван <Ромашка>"))
    )
    monkeypatch.setattr(staff_ui, "attribute_all", AsyncMock(return_value={"unresolved": 0}))
    message = SimpleNamespace(text="Иван <Ромашка>", answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())

    await staff_ui.on_name(message, state, SimpleNamespace())
    sent = message.answer.await_args.args[0]
    assert "Иван &lt;Ромашка&gt;" in sent and "<Ромашка>" not in sent


async def _audit_text(monkeypatch, edit_text):
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.bot.handlers import health
    from app.db.models import BotRole, BotUser, BotUserState

    rows = [
        SimpleNamespace(
            actor_user_id=None, action="setting.changed", object_type="setting",
            object_id="alerts", created_at=datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
            payload={"field": "threshold_minutes", "from": "&" * 80, "to": "<" * 80},
        )
        for _ in range(health.AUDIT_LIMIT)
    ]
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)),
        expunge=lambda obj: None,
    )

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(health, "session_scope", scope)
    monkeypatch.setattr(health, "get_section", AsyncMock(return_value={}))
    owner = BotUser(id=1, tg_user_id=1, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)
    query = SimpleNamespace(message=SimpleNamespace(edit_text=edit_text), answer=AsyncMock())
    await health.on_audit(query, owner)
    return query


async def test_long_audit_is_cut_by_whole_entries(monkeypatch):
    from unittest.mock import AsyncMock

    edit_text = AsyncMock()
    await _audit_text(monkeypatch, edit_text)
    text = edit_text.await_args.args[0]
    assert len(text) <= 4096
    assert _no_torn_entity(text), "журнал обрезан посреди сущности"
    assert text.count("<b>") == text.count("</b>"), "журнал обрезан посреди тега"


async def test_audit_render_failure_is_logged(monkeypatch):
    from unittest.mock import AsyncMock

    from structlog.testing import capture_logs

    edit_text = AsyncMock(side_effect=RuntimeError("Bad Request: can't parse entities"))
    with capture_logs() as logs:
        query = await _audit_text(monkeypatch, edit_text)
    assert any(entry["event"] == "audit.render_failed" for entry in logs), (
        "ошибка показа журнала проглочена молча"
    )
    query.answer.assert_awaited_once()


def test_breach_days_are_ordered_by_date_across_month_boundary():
    """Разбивка «по дням» идёт по дате: 30.09 раньше 01.10."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from app.services.html_report import _breach_details

    def item(day: datetime) -> dict:
        return {
            "title": "Чат",
            "kind": "reaction",
            "opened_at": day,
            "since": day,
            "until": day + timedelta(hours=1),
            "business_delay": 3600,
        }

    late = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    early = datetime(2026, 9, 30, 9, tzinfo=timezone.utc)
    html = _breach_details(
        [{"staff_id": 1, "full_name": "Синтетика"}],
        {1: {"count": 2, "items": [item(late), item(early)]}},
        ZoneInfo("Europe/Moscow"),
        {},
    )
    days = html.split("Просрочки по дням", 1)[1]
    assert days.index("30.09") < days.index("01.10")
