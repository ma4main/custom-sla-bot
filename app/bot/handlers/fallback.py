"""Страховочный обработчик приватных сообщений вне сценариев: без него человек
не получил бы никакого ответа.

Подключается после разделов меню (иначе перехватит ввод форм) и до приёма
групповых сообщений.
"""

from __future__ import annotations

import structlog
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from app.bot.handlers.menu import greeting
from app.bot.keyboards import main_menu
from app.db.base import session_scope
from app.db.models import BotUserState
from app.services.access import get_user, register_start
from app.text import NEUTRAL_REPLY

log = structlog.get_logger(__name__)

router = Router(name="fallback")
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.message()
async def on_any_private(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    async with session_scope() as session:
        bot_user = await get_user(session, user.id)
        if bot_user is None:
            # Человек написал боту, минуя /start — заявка ставится так же,
            # как при /start, чтобы владелец увидел её в очереди.
            bot_user = await register_start(
                session,
                tg_user_id=user.id,
                username=user.username,
                display_name=user.full_name,
            )
        session.expunge(bot_user)

    if bot_user.state is not BotUserState.ACTIVE:
        log.info("fallback.pending", tg_user_id=user.id, username=user.username)
        await message.answer(NEUTRAL_REPLY)
        return

    await message.answer(
        greeting(bot_user), reply_markup=main_menu(bot_user), parse_mode="HTML"
    )
