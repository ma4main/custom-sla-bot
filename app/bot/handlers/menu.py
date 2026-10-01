"""Главное меню и навигация."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.text import esc
from app.bot.callbacks import Nav
from app.bot.keyboards import main_menu
from app.db.models import BotUser
from app.services.access import ROLE_HINTS, role_label

router = Router(name="menu")
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


def greeting(user: BotUser) -> str:
    name = esc(user.display_name or user.username or "коллега")
    return (
        "<b>📊 Аналитика чатов</b>\n\n"
        f"{name}, здравствуйте.\n"
        f"Ваша роль: <b>{role_label(user.role)}</b> — "
        f"{ROLE_HINTS.get(user.role, '')}.\n\n"
        "Бот считает нагрузку по чатам и сотрудникам и следит за скоростью "
        "ответов клиентам. В рабочих чатах он ничего не пишет — только читает.\n\n"
        "Выберите раздел:"
    )


async def show_main(query: CallbackQuery, user: BotUser) -> None:
    await query.message.edit_text(
        greeting(user), reply_markup=main_menu(user), parse_mode="HTML"
    )
    await query.answer()


@router.callback_query(Nav.filter(F.to == "main"))
async def on_main(
    query: CallbackQuery, bot_user: BotUser, state: FSMContext | None = None
) -> None:
    # Главная — выход из любой формы, как /menu.
    if state is not None:
        await state.clear()
    await show_main(query, bot_user)


@router.callback_query(F.data == "noop")
async def on_noop(query: CallbackQuery) -> None:
    await query.answer()
