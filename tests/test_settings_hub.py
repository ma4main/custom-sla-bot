"""Раздел-хаб настроек: «Отчёты по расписанию» открывается кнопками блоков,
поля — внутри блока, «Назад» с поля ведёт в свой блок. Каждая точка входа
обязана возвращать туда, откуда пришли.
"""

from contextlib import asynccontextmanager

from app.bot.callbacks import SettingAction
from app.db.models import BotRole, BotUser, BotUserState
from tests.conftest import requires_db


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

    async def answer(self, *args, **kwargs):
        return None


def _owner() -> BotUser:
    return BotUser(
        tg_user_id=770200, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
    )


def _callbacks(query) -> list[str]:
    """Только callback-и настроек: «Назад» в корень (nav:) — не SettingAction."""
    return [
        button.callback_data
        for row in query.message.markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("set:")
    ]


def _patch_scope(monkeypatch, session):
    import app.bot.handlers.settings_ui as ui

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(ui, "session_scope", fake_scope)
    return ui


@requires_db
async def test_digest_section_opens_as_hub(session, monkeypatch):
    """Вместо простыни полей — кнопки блоков; полей на первом экране нет."""
    ui = _patch_scope(monkeypatch, session)

    query = _FakeQuery()
    await ui.on_section(
        query, SettingAction(action="section", section="digest"), _owner()
    )

    data = _callbacks(query)
    groups = [d for d in data if SettingAction.unpack(d).action == "group"]
    edits = [d for d in data if SettingAction.unpack(d).action == "edit"]
    assert len(groups) >= 4, "хаб обязан показать все блоки рассылок"
    assert not edits, "поля не должны торчать на первом экране хаба"
    assert "Недельный отчёт" in query.message.text
    assert "вкл" in query.message.text or "выкл" in query.message.text, (
        "у блока с тумблером в хабе виден статус"
    )


@requires_db
async def test_group_screen_shows_only_its_fields(session, monkeypatch):
    """Блок недельного отчёта — только weekly_*, «Назад» — в хаб."""
    ui = _patch_scope(monkeypatch, session)

    query = _FakeQuery()
    await ui.on_group(
        query, SettingAction(action="group", section="digest", key="1"), _owner()
    )

    data = _callbacks(query)
    edit_keys = {
        SettingAction.unpack(d).key
        for d in data
        if SettingAction.unpack(d).action == "edit"
    }
    from app.services.settings_store import FIELD_GROUPS

    weekly_keys = {k for title, keys in FIELD_GROUPS["digest"] for k in keys if "Недельный" in title}
    assert edit_keys == weekly_keys, "в блоке недельного — ровно его поля из FIELD_GROUPS"
    assert "weekly_period" in edit_keys and "monthly_enabled" not in edit_keys
    backs = [d for d in data if SettingAction.unpack(d).action == "section"]
    assert backs, "«Назад» из блока обязан вести в хаб раздела"


def test_field_back_leads_to_its_group():
    """С экрана поля хаба «Назад» ведёт в блок поля, не в хаб и не в корень."""
    import app.bot.handlers.settings_ui as ui

    back = SettingAction.unpack(ui._field_back_cb("digest", "monthly_html"))
    assert (back.action, back.section) == ("group", "digest")

    # Раздел без блоков — по-старому, в раздел.
    plain = SettingAction.unpack(ui._field_back_cb("alerts", "enabled"))
    assert (plain.action, plain.section) == ("section", "alerts")


class _FakeState:
    """Минимальный FSMContext: экран «действует с» кладёт в него раздел."""

    def __init__(self) -> None:
        self.data: dict = {}

    async def set_state(self, *args, **kwargs):
        return None

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return self.data

    async def clear(self):
        self.data = {}


class _FakeIncoming:
    """Сообщение пользователя с ответом бота — для формы ввода момента."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def answer(self, text, reply_markup=None, parse_mode=None, **kwargs):
        self.replies.append(text)


@requires_db
async def test_version_since_is_editable_from_the_bot(session, monkeypatch):
    """«Действует с …» правится кнопками в боте, а не миграцией."""
    from datetime import datetime, timedelta, timezone

    from app.services.settings_store import get_section, set_value
    from app.services.versioning import parse_since

    ui = _patch_scope(monkeypatch, session)
    owner = _owner()
    session.add(owner)
    await session.flush()

    # Версия появляется от правки порога — до неё двигать нечего.
    await set_value(session, "alerts", "threshold_minutes", 45, actor_id=None)
    await session.flush()

    state = _FakeState()
    query = _FakeQuery()
    await ui.on_since(
        query, SettingAction(action="since", section="alerts"), state, owner
    )
    assert "действует с" in query.message.text.lower()
    assert state.data == {"section": "alerts", "key": "__since__"}

    # Момент задним числом — принимается.
    yesterday = datetime.now(timezone(timedelta(hours=3))) - timedelta(days=1)
    incoming = _FakeIncoming(yesterday.strftime("%d.%m %H:%M"))
    await ui.on_value(incoming, state, owner)
    await session.flush()

    values = await get_section(session, "alerts")
    saved = parse_since(values["since"])
    assert saved is not None
    assert abs((saved - yesterday).total_seconds()) < 120, incoming.replies
    assert values["threshold_minutes"] == 45

    # Будущее — отклоняется, значение не меняется.
    state = _FakeState()
    await state.update_data(section="alerts", key="__since__")
    tomorrow = datetime.now(timezone(timedelta(hours=3))) + timedelta(days=1)
    incoming = _FakeIncoming(tomorrow.strftime("%d.%m %H:%M"))
    await ui.on_value(incoming, state, owner)
    assert incoming.replies and "❌" in incoming.replies[0]
    assert (await get_section(session, "alerts"))["since"] == values["since"]


@requires_db
async def test_audit_screen_shows_who_changed_what(session, monkeypatch):
    """Журнал действий в боте: кто, когда, какую настройку."""
    import app.bot.handlers.health as health
    from app.bot.callbacks import Nav
    from app.db.models import AuditLog
    from app.services.settings_store import set_value

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(health, "session_scope", fake_scope)

    owner_user = BotUser(
        tg_user_id=770900, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE,
        display_name="Дмитрий",
    )
    session.add(owner_user)
    await session.flush()
    await set_value(session, "work_calendar", "end", "17:00", actor_id=owner_user.id)
    await session.flush()
    assert await session.scalar(
        __import__("sqlalchemy").select(AuditLog.id).where(AuditLog.action == "setting.changed")
    )

    query = _FakeQuery()
    await health.on_audit(query, owner_user)
    text = query.message.text
    assert "Журнал действий" in text
    assert "Дмитрий" in text and "изменил настройку" in text
    assert "17:00" in text, text
    callbacks = [
        b.callback_data for row in query.message.markup.inline_keyboard for b in row if b.callback_data
    ]
    assert Nav(to="health").pack() in callbacks
