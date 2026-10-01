"""Настоящий middleware справки: активные роли проходят, остальные — нет."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.types import CallbackQuery, Message, User

from app.bot import middlewares
from app.bot.callbacks import HelpNav
from app.bot.handlers import help_ui
from app.bot.main import _PROTECTED
from app.db.models import BotRole, BotUser, BotUserState
from app.services.help_text import HelpContext


@pytest.mark.parametrize("role", list(BotRole))
@pytest.mark.parametrize("state", [*BotUserState, None])
async def test_help_callback_requires_active_account(monkeypatch, role, state):
    account = (BotUser(tg_user_id=123, role=role, state=state, permissions={})
               if state is not None else None)

    @asynccontextmanager
    async def session_scope():
        yield Mock()

    monkeypatch.setattr(middlewares, "session_scope", session_scope)
    monkeypatch.setattr(middlewares, "get_user", AsyncMock(return_value=account))
    load_context = AsyncMock(return_value=HelpContext())
    monkeypatch.setattr(help_ui, "load_context", load_context)
    edit = AsyncMock()
    answer = AsyncMock()
    monkeypatch.setattr(Message, "edit_text", edit)
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    telegram_user = User(id=123, is_bot=False, first_name="Тест")
    query = CallbackQuery.model_validate({
        "id": "help-access", "from": telegram_user, "chat_instance": "test",
        "data": HelpNav(page="episode").pack(),
        "message": {"message_id": 1, "date": 0,
                    "chat": {"id": 123, "type": "private"}, "text": "Справка"},
    })

    async def handler(event, data):
        await help_ui.on_help_page(event, HelpNav(page="episode"), data["bot_user"])

    # Проверяем именно право, зарегистрированное для роутера в приложении.
    required = dict(_PROTECTED)[help_ui.router]
    await middlewares.AuthMiddleware(required)(handler, query, {"event_from_user": telegram_user})
    answer.assert_awaited_once()
    if state is BotUserState.ACTIVE:
        load_context.assert_awaited_once_with(account)
        edit.assert_awaited_once()
        assert "Обращения" in edit.call_args.args[0]
    else:
        load_context.assert_not_awaited()
        edit.assert_not_awaited()
