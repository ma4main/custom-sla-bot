"""Один ответ на нажатие: обработчик, показавший всплывающее сообщение, перерисовывает
экран без второго answerCallbackQuery (Telegram его отвергает, и в логе — сбой обработчика).

Экраны подменены заглушками, которые, как настоящие, отвечают на нажатие сами;
база и сервисы — подставные. Здесь же — ошибки формы написания имени и кнопка
«Включить» в карточке пользователя.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.callbacks import ChatAction, MarkupAction, SenderAction, StaffAction, UserAction
from app.bot.handlers import chats, sender_ui, staff_ui, users
from app.db.models import BotRole, BotUser, BotUserState, SenderRuleKind

OWNER = BotUser(id=1, tg_user_id=1, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)


class TelegramQuery:
    """Нажатие как его видит Telegram: на него отвечают один раз."""

    def __init__(self) -> None:
        self.answers: list[tuple] = []
        self.message = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())

    async def answer(self, *args, **kwargs) -> None:
        self.answers.append((args, kwargs))
        if len(self.answers) > 1:
            raise AssertionError(f"второй ответ на то же нажатие: {self.answers}")


async def _renders_itself(query, *args, **kwargs) -> None:
    await query.answer()


def _fake_db(monkeypatch, module, obj) -> None:
    session = SimpleNamespace(
        get=AsyncMock(return_value=obj),
        scalar=AsyncMock(return_value=obj),
        add=lambda row: None,
        expunge=lambda row: None,
    )

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(module, "session_scope", scope)


def _target(**extra) -> SimpleNamespace:
    fields = dict(
        id=5, tg_user_id=5, role=BotRole.MANAGER, state=BotUserState.ACTIVE,
        notify_personal=False, full_name="Синтетика", staff_id=None, raw_name="Синтетика",
    )
    return SimpleNamespace(**{**fields, **extra})


def _users(monkeypatch, service: str | None):
    _fake_db(monkeypatch, users, _target())
    if service:
        monkeypatch.setattr(users, service, AsyncMock())
    monkeypatch.setattr(users, "_render_list", _renders_itself)
    monkeypatch.setattr(users, "on_view", _renders_itself)


CASES = [
    ("users.notify", lambda mp: _users(mp, None),
     lambda q: users.on_notify_toggle(q, UserAction(action="notify", user_id=1), OWNER)),
    ("users.set_role", lambda mp: _users(mp, "change_role"),
     lambda q: users.on_set_role(q, UserAction(action="set_role", user_id=5, role="admin"), OWNER)),
    ("users.disable", lambda mp: _users(mp, "disable_user"),
     lambda q: users.on_disable(q, UserAction(action="disable", user_id=5), OWNER)),
    ("users.enable", lambda mp: _users(mp, "enable_user"),
     lambda q: users.on_enable(q, UserAction(action="enable", user_id=5), OWNER)),
    ("users.co_owner", lambda mp: _users(mp, "promote_to_owner"),
     lambda q: users.on_co_owner(q, UserAction(action="co_owner_do", user_id=5), OWNER)),
    ("users.transfer", lambda mp: _users(mp, "transfer_ownership"),
     lambda q: users.on_transfer(q, UserAction(action="transfer_do", user_id=5), OWNER)),
    ("users.approve", lambda mp: _users(mp, "approve_user"),
     lambda q: users.on_approve(q, UserAction(action="approve", user_id=5, role="admin"), OWNER)),
    ("users.link_do", lambda mp: _users(mp, "link_staff"),
     lambda q: users.on_link_do(q, UserAction(action="link_do", user_id=5, role="3"), OWNER)),
]


def _staff(monkeypatch, service: str | None, obj=None, returns=1):
    _fake_db(monkeypatch, staff_ui, obj if obj is not None else _target())
    if service:
        monkeypatch.setattr(staff_ui, service, AsyncMock(return_value=returns))
    for name in ("_render_card", "_render_unresolved", "_render_ignored"):
        monkeypatch.setattr(staff_ui, name, _renders_itself)


CASES += [
    ("staff.role_set", lambda mp: _staff(mp, "set_manual_role"),
     lambda q: staff_ui.on_role_set(q, StaffAction(action="role_s", staff_id=5), OWNER)),
    ("staff.toggle", lambda mp: _staff(mp, "set_active"),
     lambda q: staff_ui.on_toggle(q, StaffAction(action="deactivate", staff_id=5), OWNER)),
    ("markup.bind", lambda mp: _staff(mp, "bind_raw_name"),
     lambda q: staff_ui.on_markup_bind(q, MarkupAction(action="bind", msg_id=7, staff_id=5), OWNER)),
    ("markup.ignore", lambda mp: _staff(mp, "mark_not_staff"),
     lambda q: staff_ui.on_markup_ignore(q, MarkupAction(action="ignore", msg_id=7), OWNER)),
    ("markup.restore", lambda mp: _staff(mp, "restore_to_queue"),
     lambda q: staff_ui.on_markup_restore(q, MarkupAction(action="restore", msg_id=7), OWNER)),
    # Подпись уже разобрали, пока открывали: «Уже разобрано» и очередь.
    ("markup.pick_done", lambda mp: _staff(mp, None, obj=_target(staff_id=3)),
     lambda q: staff_ui.on_markup_pick(q, MarkupAction(action="pick", msg_id=7), OWNER)),
]


def _ignored_missing(monkeypatch):
    _staff(monkeypatch, None)
    _fake_db(monkeypatch, staff_ui, None)


CASES += [
    ("markup.iview_missing", _ignored_missing,
     lambda q: staff_ui.on_markup_ignored_view(q, MarkupAction(action="iview", msg_id=7), OWNER)),
]


def _sender(monkeypatch, service: str, returns):
    _fake_db(monkeypatch, sender_ui, _target())
    monkeypatch.setattr(sender_ui, "_kind_of", AsyncMock(return_value=SenderRuleKind.TG_USER))
    monkeypatch.setattr(sender_ui, "display_for", AsyncMock(return_value="Синтетика"))
    monkeypatch.setattr(sender_ui, "load_rule", AsyncMock(return_value=object()))
    monkeypatch.setattr(sender_ui, service, AsyncMock(return_value=returns))
    monkeypatch.setattr(sender_ui, "_render_card", _renders_itself)


CASES += [
    ("sender.decide", lambda mp: _sender(mp, "set_rule", (None, {"messages": 1, "changed": 0})),
     lambda q: sender_ui.on_decide(q, SenderAction(action="cli", key=77), OWNER)),
    ("sender.reset", lambda mp: _sender(mp, "clear_rule", {"messages": 1}),
     lambda q: sender_ui.on_reset(q, SenderAction(action="del", key=77), OWNER)),
]


def _chats(monkeypatch, service: str, returns=None):
    _fake_db(monkeypatch, chats, _target())
    monkeypatch.setattr(chats, service, AsyncMock(return_value=returns))
    monkeypatch.setattr(chats, "on_chats", _renders_itself)
    monkeypatch.setattr(chats, "_render_card", _renders_itself)


CASES += [
    ("chats.track_all", lambda mp: _chats(mp, "track_all_discovered", 2),
     lambda q: chats.on_track_all(q, OWNER)),
    ("chats.state", lambda mp: _chats(mp, "set_state"),
     lambda q: chats.on_state_change(q, ChatAction(action="pause", chat_id=5), OWNER)),
]


@pytest.mark.parametrize("name,prepare,press", CASES, ids=[case[0] for case in CASES])
async def test_one_answer_per_press(monkeypatch, name, prepare, press):
    prepare(monkeypatch)
    query = TelegramQuery()
    await press(query)
    assert len(query.answers) == 1, f"{name}: ответов {len(query.answers)}"


# ── Форма написания имени ─────────────────────────────────────────────────
async def test_alias_error_is_shown_not_raised(monkeypatch):
    from app.services.staff import StaffError

    _fake_db(monkeypatch, staff_ui, _target())
    monkeypatch.setattr(
        staff_ui, "add_alias", AsyncMock(side_effect=StaffError("Пустой вариант написания"))
    )
    message = SimpleNamespace(text="  ", answer=AsyncMock())
    state = SimpleNamespace(get_data=AsyncMock(return_value={"staff_id": 5}), clear=AsyncMock())

    await staff_ui.on_alias_value(message, state, OWNER)
    message.answer.assert_awaited_once_with("❌ Пустой вариант написания")
    state.clear.assert_not_awaited()


# ── Кнопка «Включить» ─────────────────────────────────────────────────────
def _card_actions(state: BotUserState) -> set[str]:
    from app.bot.keyboards import user_card

    person = BotUser(id=5, tg_user_id=5, role=BotRole.MANAGER, permissions={}, state=state)
    markup = user_card(person, 0, is_self=False, can_manage=True)
    return {
        UserAction.unpack(button.callback_data).action
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("user:")
    }


def test_disabled_user_card_offers_enable_instead_of_disable():
    disabled = _card_actions(BotUserState.DISABLED)
    assert "enable" in disabled and "disable" not in disabled
    active = _card_actions(BotUserState.ACTIVE)
    assert "disable" in active and "enable" not in active


async def test_enable_handler_reports_access_error(monkeypatch):
    from app.services.access import AccessError

    _fake_db(monkeypatch, users, _target())
    monkeypatch.setattr(
        users, "enable_user", AsyncMock(side_effect=AccessError("Пользователь не отключён"))
    )
    render = AsyncMock()
    monkeypatch.setattr(users, "_render_list", render)
    query = TelegramQuery()

    await users.on_enable(query, UserAction(action="enable", user_id=5), OWNER)
    assert query.answers == [(("Пользователь не отключён",), {"show_alert": True})]
    render.assert_not_awaited()
