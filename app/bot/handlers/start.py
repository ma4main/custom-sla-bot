"""Приватный диалог: /start и регистрация.

Незарегистрированный получает нейтральную фразу `NEUTRAL_REPLY` без упоминания
доступа (`/start` может нажать клиент), а заявка ставится в очередь; тем, кто
управляет пользователями, уходит уведомление о новой заявке (docs/SCREENS.md §1).
"""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.handlers.menu import greeting
from app.bot.keyboards import main_menu
from app.db.base import session_scope
from app.db.models import BotUserState
from app.services.access import (
    Perm,
    get_user,
    notification_recipients,
    register_start,
)
from app.text import NEUTRAL_REPLY, esc

log = structlog.get_logger(__name__)

router = Router(name="start")
router.message.filter(F.chat.type == ChatType.PRIVATE)


async def _notify_owners_about_request(message: Message, user) -> None:
    """Сообщить о заявке всем, у кого есть право управлять пользователями."""
    async with session_scope() as session:
        people = await notification_recipients(session, Perm.USER_MANAGE)
        recipients = [person.tg_user_id for person in people]

    name = esc(user.full_name or user.username or str(user.id))
    handle = f" (@{esc(user.username)})" if user.username else ""
    text = (
        "🔔 <b>Новая заявка на доступ</b>\n\n"
        f"{name}{handle}\n"
        f"Telegram ID: <code>{user.id}</code>\n\n"
        "Выдать роль: «🛡 Пользователи бота» → «📥 Заявки на доступ»."
    )
    for tg_user_id in recipients:
        if tg_user_id == user.id:
            continue
        try:
            await message.bot.send_message(tg_user_id, text, parse_mode="HTML")
        except Exception:  # noqa: BLE001 — недоставленное не ломает регистрацию
            log.exception("start.notify_failed", tg_user_id=tg_user_id)


@router.message(Command("menu"))
@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def on_start(
    message: Message,
    command: CommandObject | None = None,
    state: FSMContext | None = None,
) -> None:
    user = message.from_user
    if user is None:
        return
    # /menu и /start — выход из любой формы: иначе брошенный ввод съел бы
    # следующее сообщение.
    if state is not None:
        await state.clear()

    invite_code = (command.args or "").strip() if command else None

    async with session_scope() as session:
        existing = await get_user(session, user.id)
        is_new = existing is None
        bot_user = await register_start(
            session,
            tg_user_id=user.id,
            username=user.username,
            display_name=user.full_name,
            invite_code=invite_code or None,
        )
        session.expunge(bot_user)
        user_state = bot_user.state
        role = bot_user.role

    if user_state is not BotUserState.ACTIVE:
        log.info("start.pending", tg_user_id=user.id, username=user.username)
        await message.answer(NEUTRAL_REPLY)
        if is_new:
            await _notify_owners_about_request(message, user)
        return

    log.info("start.active", tg_user_id=user.id, role=role.value)
    await message.answer(
        greeting(bot_user), reply_markup=main_menu(bot_user), parse_mode="HTML"
    )
