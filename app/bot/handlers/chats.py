"""Раздел «Чаты»: включение в анализ, пауза, архив."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message as TgMessage

from app.text import esc
from app.bot.callbacks import ChatAction, Nav
from app.bot.handlers import AnsweredAlready
from app.bot.keyboards import (
    back_to,
    chat_card,
    chats_list,
    chats_overview,
    chats_search_results,
    confirm,
)
from app.db.base import session_scope
from app.db.models import BotUser, Chat, ChatState
from app.services.access import AccessError
from app.services.chats import (
    STATE_LABELS,
    ChatError,
    count_by_state,
    delete_chat,
    list_chats,
    message_count,
    search_chats,
    set_state,
    track_all_discovered,
)

router = Router(name="chats")
# Только приватные диалоги: в группы бот не пишет и меню там не показывает.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

PER_PAGE = 8
SEARCH_LIMIT = 10


class ChatSearchForm(StatesGroup):
    waiting_query = State()


@router.callback_query(Nav.filter(F.to == "chats"))
async def on_chats(
    query: CallbackQuery, bot_user: BotUser, state: FSMContext | None = None
) -> None:
    # «Назад» из формы поиска ведёт сюда — ожидание текста надо снять,
    # иначе следующее сообщение человека утечёт в брошенную форму.
    if state is not None:
        await state.clear()
    async with session_scope() as session:
        counts = await count_by_state(session)

    total = sum(counts.values())
    discovered = counts.get(ChatState.DISCOVERED, 0)

    text = (
        "<b>💬 Чаты</b>\n\n"
        f"Всего известно боту: <b>{total}</b>\n\n"
        "Новый чат включается в анализ сам, как только бота добавят в группу.\n\n"
        "⏸ <b>Пауза</b> — алерты по чату молчат, чат уходит из «Требует "
        "внимания» и из сводок по алертам.\n"
        "Чат остаётся в списках, отчёты за прошлое сохраняются; время "
        "на паузе в них не входит.\n\n"
        "📦 <b>Архив</b> — чат уходит из активных списков.\n"
        "Туда же он попадает сам, если бота удалили из группы. "
        "Из архива чат можно удалить совсем."
    )
    if discovered:
        text += (
            "\n\n⚠️ <b>Есть обнаруженные чаты.</b>\n"
            "В отчёты они не попадают, пока вы не включите их в анализ. "
            "Сообщения в них при этом пишутся."
        )
    await query.message.edit_text(text, reply_markup=chats_overview(counts), parse_mode="HTML")
    await query.answer()


@router.callback_query(ChatAction.filter(F.action == "track_all"))
async def on_track_all(query: CallbackQuery, bot_user: BotUser) -> None:
    async with session_scope() as session:
        try:
            count = await track_all_discovered(session, bot_user)
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer(f"Включено чатов: {count}", show_alert=True)
    await on_chats(AnsweredAlready(query), bot_user)


@router.callback_query(ChatAction.filter(F.action == "search"))
async def on_search(query: CallbackQuery, state: FSMContext, bot_user: BotUser) -> None:
    await state.set_state(ChatSearchForm.waiting_query)
    await query.message.edit_text(
        "<b>🔍 Поиск чата</b>\n\n"
        "Отправьте часть названия сообщением — например <code>ромаш</code>.\n\n"
        "<i>Регистр не важен. Ищется по всем чатам: в анализе, на паузе, "
        "в архиве.</i>",
        reply_markup=back_to("chats"),
        parse_mode="HTML",
    )
    await query.answer()


@router.message(ChatSearchForm.waiting_query)
async def on_search_query(message: TgMessage, state: FSMContext, bot_user: BotUser) -> None:
    needle = (message.text or "").strip()
    if not needle:
        await message.answer(
            "Пустой запрос — отправьте часть названия или вернитесь кнопкой «Назад»."
        )
        return

    await state.clear()
    async with session_scope() as session:
        chats, total = await search_chats(session, needle, limit=SEARCH_LIMIT)

    if not chats:
        text = f"<b>🔍 «{esc(needle)}»</b>\n\nНичего не нашлось."
    elif total > len(chats):
        text = (
            f"<b>🔍 «{esc(needle)}»</b>\n\nСовпадений: {total}, показаны первые "
            f"{len(chats)} — уточните запрос."
        )
    else:
        text = f"<b>🔍 «{esc(needle)}»</b>\n\nНайдено: {total}"

    await message.answer(
        text, reply_markup=chats_search_results(chats), parse_mode="HTML"
    )


@router.callback_query(ChatAction.filter(F.action == "list"))
async def on_list(query: CallbackQuery, callback_data: ChatAction, bot_user: BotUser) -> None:
    state = ChatState(callback_data.value)
    page = callback_data.page

    async with session_scope() as session:
        chats, total = await list_chats(
            session, state, offset=page * PER_PAGE, limit=PER_PAGE
        )

    if not chats:
        text = f"<b>{STATE_LABELS[state]}</b>\n\nПусто."
    else:
        text = f"<b>{STATE_LABELS[state]}</b>\n\nВсего: {total}"

    await query.message.edit_text(
        text, reply_markup=chats_list(chats, state, page, total, PER_PAGE), parse_mode="HTML"
    )
    await query.answer()


async def _render_card(query: CallbackQuery, chat_id: int, page: int) -> None:
    async with session_scope() as session:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            await query.answer("Чат не найден", show_alert=True)
            return
        messages = await message_count(session, chat.id)
        session.expunge(chat)

    # Время — в рабочем поясе, как везде в боте.
    from zoneinfo import ZoneInfo

    from app.config import get_settings

    tz = ZoneInfo(get_settings().tz)
    tracked = (
        chat.tracked_since.astimezone(tz).strftime("%d.%m.%Y %H:%M")
        if chat.tracked_since
        else "не включался"
    )
    lines = [
        f"<b>{esc(chat.title or 'без названия')}</b>\n",
        f"Состояние: {STATE_LABELS[chat.state]}",
        f"Сообщений собрано: {messages}",
        f"В анализе с: {tracked}",
    ]
    if chat.is_forum:
        lines.append("Форум с темами: да — обращения считаются по темам отдельно")

    await query.message.edit_text(
        "\n".join(lines), reply_markup=chat_card(chat, page), parse_mode="HTML"
    )
    await query.answer()


@router.callback_query(ChatAction.filter(F.action == "view"))
async def on_view(query: CallbackQuery, callback_data: ChatAction, bot_user: BotUser) -> None:
    await _render_card(query, callback_data.chat_id, callback_data.page)


@router.callback_query(ChatAction.filter(F.action == "delete"))
async def on_delete_confirm(
    query: CallbackQuery, callback_data: ChatAction, bot_user: BotUser
) -> None:
    async with session_scope() as session:
        chat = await session.get(Chat, callback_data.chat_id)
        if chat is None:
            await query.answer("Чат не найден", show_alert=True)
            return
        messages = await message_count(session, chat.id)
        session.expunge(chat)

    text = (
        f"<b>Удалить чат «{esc(chat.title or 'без названия')}»?</b>\n\n"
        f"Будет удалено: {messages} собранных сообщений и вся статистика "
        "по этому чату — обращения, скорость ответов, авторство.\n\n"
        "<b>Отменить это нельзя.</b> Чат пропадёт из всех списков и отчётов, "
        "в том числе за прошлые периоды.\n\n"
        "<i>Если бота потом снова добавят в эту группу, чат появится заново — "
        "но уже пустым.</i>"
    )
    await query.message.edit_text(
        text,
        reply_markup=confirm(
            ChatAction(
                action="delete_do", chat_id=chat.id, page=callback_data.page
            ).pack(),
            ChatAction(
                action="view", chat_id=chat.id, page=callback_data.page
            ).pack(),
            label="🗑 Да, удалить",
        ),
        parse_mode="HTML",
    )
    await query.answer()


@router.callback_query(ChatAction.filter(F.action == "delete_do"))
async def on_delete(query: CallbackQuery, callback_data: ChatAction, bot_user: BotUser) -> None:
    async with session_scope() as session:
        actor = await session.get(BotUser, bot_user.id)
        chat = await session.get(Chat, callback_data.chat_id)
        if chat is None or actor is None:
            await query.answer("Чат не найден", show_alert=True)
            return
        try:
            summary = await delete_chat(session, actor, chat)
        except (AccessError, ChatError) as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer("Чат удалён")
    text = (
        f"<b>Чат удалён</b>\n\n"
        f"«{esc(summary['title'] or 'без названия')}» — вместе с ним удалено "
        f"{summary['messages']} сообщений и вся статистика по нему."
    )
    await query.message.edit_text(
        text,
        reply_markup=back_to("chats"),
        parse_mode="HTML",
    )


_STATE_BY_ACTION = {
    "track": ChatState.TRACKED,
    "pause": ChatState.PAUSED,
    "archive": ChatState.ARCHIVED,
}

_STATE_DONE = {
    ChatState.TRACKED: "Чат включён в анализ",
    ChatState.PAUSED: "Чат на паузе: алерты по нему молчат, отчёты за прошлое сохранены",
    ChatState.ARCHIVED: "Чат в архиве: ушёл из активных списков. Оттуда его можно удалить совсем",
}


@router.callback_query(ChatAction.filter(F.action.in_(set(_STATE_BY_ACTION))))
async def on_state_change(
    query: CallbackQuery, callback_data: ChatAction, bot_user: BotUser
) -> None:
    target = _STATE_BY_ACTION[callback_data.action]

    async with session_scope() as session:
        chat = await session.get(Chat, callback_data.chat_id)
        if chat is None:
            await query.answer("Чат не найден", show_alert=True)
            return
        try:
            await set_state(session, bot_user, chat, target)
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer(_STATE_DONE[target], show_alert=target is ChatState.ARCHIVED)
    await _render_card(AnsweredAlready(query), callback_data.chat_id, callback_data.page)
