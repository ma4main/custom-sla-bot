"""Раздел «📖 Справка»: оглавление и экраны из help_text.

Тексты собираются из настроек в момент показа — справка показывает действующие значения.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.callbacks import HelpNav, Nav
from app.config import effective_notify_group_id, get_settings
from app.db.base import session_scope
from app.db.models import BotUser
from app.services import help_text
from app.services.access import Perm, has_perm
from app.services.settings_store import get_section

router = Router(name="help")
# Только приватные диалоги: в группы бот не пишет и меню там не показывает.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


def hub_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, title in help_text.PAGES:
        builder.button(text=title, callback_data=HelpNav(page=key).pack())
    builder.button(text="‹ В меню", callback_data=Nav(to="main").pack())
    builder.adjust(1)
    return builder.as_markup()


def page_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="‹ К справке", callback_data=HelpNav(page="hub").pack())
    builder.button(text="‹ В меню", callback_data=Nav(to="main").pack())
    builder.adjust(1)
    return builder.as_markup()


async def load_context(user: BotUser) -> help_text.HelpContext:
    settings = get_settings()
    async with session_scope() as session:
        ctx = help_text.HelpContext(
            calendar=await get_section(session, "work_calendar"),
            alerts=await get_section(session, "alerts"),
            episodes=await get_section(session, "episodes"),
            digest=await get_section(session, "digest"),
            ai_enabled=bool(settings.ai_enabled),
            notify_group_configured=effective_notify_group_id() is not None,
            can_review_authors=has_perm(user, Perm.STAFF_MANAGE),
            can_review_alerts=has_perm(user, Perm.REPORT_ALL_CHATS),
        )
    return ctx


async def _show(query: CallbackQuery, page: str, user: BotUser) -> None:
    ctx = await load_context(user)
    if page == "hub" or page not in help_text.PAGE_TITLES:
        text, markup = help_text.hub(ctx), hub_keyboard()
    else:
        text, markup = help_text.render(page, ctx), page_keyboard()
    try:
        await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as error:
        # Повторное нажатие той же кнопки: текст не изменился — не ошибка.
        if "message is not modified" not in str(error):
            raise
    await query.answer()


@router.callback_query(Nav.filter(F.to == "help"))
async def on_help(query: CallbackQuery, bot_user: BotUser) -> None:
    await _show(query, "hub", bot_user)


@router.callback_query(HelpNav.filter())
async def on_help_page(
    query: CallbackQuery, callback_data: HelpNav, bot_user: BotUser
) -> None:
    await _show(query, callback_data.page, bot_user)
