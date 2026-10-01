"""Экраны бота: журнал действий по-русски, листание выбора цели отчёта,
выход из формы поиска по кнопке раздела.

База подставная: проверяется только то, что видит человек.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot.callbacks import ReportAction
from app.bot.handlers import AnsweredAlready, health, reports, users
from app.db.models import BotRole, BotUser, BotUserState

OWNER = BotUser(
    id=1, tg_user_id=1, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
)
APP = Path(__file__).resolve().parents[1] / "app"
MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _written_actions() -> set[str]:
    """Ключи действий, которые код пишет в журнал: `action=` внутри AuditLog(...)
    и прямые вставки в audit_log из миграций."""
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for block in re.findall(r"AuditLog\((.*?)\n\s*\)", text, flags=re.DOTALL):
            for line in re.findall(r"action=([^\n]+)", block):
                found.update(re.findall(r'"([a-z_]+\.[a-z_]+)"', line))
    for path in MIGRATIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "INSERT INTO audit_log" in text:
            found.update(re.findall(r"'([a-z_]+\.[a-z_]+)'", text))
    return found


def test_every_written_audit_action_has_a_label():
    written = _written_actions()
    assert "staff.deactivated" in written, "разбор кода не нашёл тернарную запись"
    assert "sysadmin.revoked" in written, "разбор миграций не нашёл запись"
    missing = sorted(written - set(health._AUDIT_LABELS))
    assert not missing, f"в журнале покажутся сырые ключи: {missing}"


def test_audit_labels_have_no_dead_keys():
    dead = sorted(set(health._AUDIT_LABELS) - _written_actions())
    assert not dead, f"подписи для действий, которые нигде не пишутся: {dead}"


def test_audit_details_are_in_russian():
    row = SimpleNamespace(
        action="chat.state_changed",
        object_type="chat",
        object_id="7",
        payload={"from": "tracked", "to": "paused", "title": "Ромашка <ООО>"},
    )
    details = health._audit_details(row)
    assert "название: Ромашка &lt;ООО&gt;" in details
    assert "было: tracked" in details and "стало: paused" in details
    assert "объект: чат №7" in details
    assert "title" not in details and "#" not in details


def test_audit_details_show_unknown_object_type_as_is():
    row = SimpleNamespace(
        action="x.y", object_type="gadget", object_id="notify_group", payload={}
    )
    assert health._audit_details(row) == "объект: gadget «notify_group»"


async def test_picker_counter_is_a_noop_in_one_row(monkeypatch):
    """Счётчик «N/M» не шлёт переход на ту же страницу: Telegram отверг бы
    правку «не изменилось». Стрелки и счётчик — одной строкой."""
    chats = [SimpleNamespace(id=i, title=f"Чат {i}", tg_chat_id=-i) for i in range(8)]
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=20),
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: chats)),
    )

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(reports, "session_scope", scope)
    query = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )
    await reports._render_picker(query, "chat", page=1)

    markup = query.message.edit_text.await_args.kwargs["reply_markup"]
    nav = [row for row in markup.inline_keyboard if any(b.text == "2/3" for b in row)]
    assert len(nav) == 1, "счётчик не найден или разнесён по строкам"
    assert [b.text for b in nav[0]] == ["‹", "2/3", "›"]
    assert nav[0][1].callback_data == "noop"
    back = ReportAction.unpack(nav[0][0].callback_data)
    forward = ReportAction.unpack(nav[0][2].callback_data)
    assert (back.action, back.target_id) == ("pick", 0)
    assert (forward.action, forward.target_id) == ("pick", 2)
    # Цели — по одной в строке, «Назад» — отдельной строкой.
    assert all(len(row) == 1 for row in markup.inline_keyboard if row is not nav[0])


async def test_users_section_drops_pending_search(monkeypatch):
    """«‹ Назад» из поиска по справочнику ведёт в раздел: ожидание текста
    снимается, иначе следующее сообщение ушло бы в поиск."""
    state = SimpleNamespace(clear=AsyncMock())
    render = AsyncMock()
    monkeypatch.setattr(users, "_render_list", render)
    query = SimpleNamespace(message=SimpleNamespace(), answer=AsyncMock())
    await users.on_users(query, state, OWNER)
    state.clear.assert_awaited_once()
    render.assert_awaited_once()


async def test_answered_already_wraps_a_message():
    message = SimpleNamespace(answer=AsyncMock())
    wrapper = AnsweredAlready(message=message)
    assert wrapper.message is message
    assert await wrapper.answer("игнорируется") is None
