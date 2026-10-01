"""Кнопки под алертами: переписка вокруг обращения.

Только чтение из своей базы — в клиентский чат бот при этом не заходит и не пишет.
"""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.callbacks import AlertAction, SenderAction
from app.db.base import session_scope
from app.db.models import BotUser, Chat, Message
from app.services.access import AccessError, Perm, has_perm
from app.services.dismissals import dismiss, is_dismissed, restore
from app.services.settings_store import get_section
from app.services.transcript import (
    calendar_tz,
    client_numbers,
    escape,
    load_around,
    render_transcript,
)

router = Router(name="alerts_ui")
# Только приватные диалоги: в группы бот не пишет и меню там не показывает.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

BEFORE = 3
AFTER = 15
# Сколько сообщений показывать при листании в одну сторону.
WINDOW = 18

# Подписи под выпиской: выписка показывает всех участников чата, значки
# объясняют, чья это сторона.
_SIDES_LEGEND = (
    "⚙️ — ответ самого Битрикса (человека за ним нет).\n"
    "⚪ — сторона не определена: чужой бот или неизвестный участник.\n"
    "«👥 Участники» — кто здесь писал и кем бот его считает; там же это "
    "можно поправить."
)

_TAILS = {
    "ctx": f"▶️ — обращение, из-за которого пришёл алерт.\n{_SIDES_LEGEND}",
    "ctxm": f"▶️ — сообщение, автора которого не узнали.\n{_SIDES_LEGEND}",
}


def _nav(
    chat_id: int, rows: list, extra_buttons: list[tuple[str, str]] | None = None
) -> InlineKeyboardMarkup:
    """Кнопки листания. Смещение не хранится: каждая кнопка несёт id сообщения,
    от которого продолжать, — листание работает и со старого сообщения."""
    builder = InlineKeyboardBuilder()
    if rows:
        builder.button(
            text="‹ Раньше",
            callback_data=AlertAction(
                action="ctxb", chat_id=chat_id, msg_id=rows[0][0].id
            ).pack(),
        )
        builder.button(
            text="Позже ›",
            callback_data=AlertAction(
                action="ctxf", chat_id=chat_id, msg_id=rows[-1][0].id
            ).pack(),
        )
        # «Участники»: границы окна кнопка несёт с собой — список отвечает ровно
        # про показанный кусок.
        builder.button(
            text="👥 Участники",
            callback_data=SenderAction(
                action="parts", msg_id=rows[0][0].id, to_id=rows[-1][0].id
            ).pack(),
        )
    # Выписка — временное окно: посмотрел и закрыл. Кнопки «в отчёт» нет: срез
    # и период в её схему не помещаются, а новое поле сломало бы разосланные алерты.
    for text_label, packed in extra_buttons or []:
        builder.button(text=text_label, callback_data=packed)
    builder.button(
        text="✖️ Закрыть переписку",
        callback_data=AlertAction(action="ctxx", chat_id=chat_id).pack(),
    )
    # Первая строка — листание в две кнопки; дальше по одной: «Участники»
    # (только когда есть что показывать), решения и закрытие.
    singles = (1 if rows else 0) + len(extra_buttons or []) + 1
    builder.adjust(*(([2] if rows else []) + [1] * singles))
    return builder.as_markup()


async def _render(
    query: CallbackQuery,
    callback_data: AlertAction,
    *,
    before: int,
    after: int,
    edit: bool,
    tail: str,
    extra_buttons: list[tuple[str, str]] | None = None,
) -> None:
    async with session_scope() as session:
        anchor = await session.get(Message, callback_data.msg_id)
        chat = await session.get(Chat, callback_data.chat_id)
        if anchor is None or chat is None:
            # На первом показе callback уже отвечен («Собираю переписку…»),
            # и второй query.answer Telegram отвергает — поэтому текстом.
            missing = "Сообщение не найдено — возможно, чат удалён."
            if edit:
                await query.answer(missing, show_alert=True)
            else:
                await query.message.answer(missing)
            return

        rows = await load_around(
            session,
            chat_id=callback_data.chat_id,
            thread_id=anchor.thread_id,
            anchor_message_id=callback_data.msg_id,
            # Темы есть только в форумах: в обычной группе thread_id у якоря
            # означает лишь реплай.
            use_thread=chat.is_forum,
            before=before,
            after=after,
        )
        numbers = await client_numbers(session, callback_data.chat_id)
        calendar_cfg = await get_section(session, "work_calendar")

    if not rows:
        if edit:
            await query.answer("Дальше сообщений нет", show_alert=True)
        else:
            await query.message.answer("Показать нечего: сообщения не сохранились.")
        return

    now = datetime.now(timezone.utc)
    body = render_transcript(
        rows,
        calendar_tz(calendar_cfg),
        now,
        # Пометка якоря — только на первом экране: при листании она
        # указывала бы на случайное сообщение с края куска.
        anchor_message_id=callback_data.msg_id if edit is False else None,
        client_numbers=numbers,
    )
    text = f"<b>{escape(chat.title or '?')}</b>\n{body}"
    if tail:
        text += f"\n\n<i>{tail}</i>"

    markup = _nav(callback_data.chat_id, rows, extra_buttons)
    if edit:
        # Листание правит то же сообщение: новые сообщения уводили бы диалог вниз.
        try:
            await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            await query.answer()
        except TelegramBadRequest:
            # Край переписки: Telegram отвергает правку «не изменилось» — дальше
            # сообщений нет.
            await query.answer("Дальше сообщений нет")
    else:
        await query.message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(AlertAction.filter(F.action.in_({"ctx", "ctxm"})))
async def on_context(
    query: CallbackQuery, callback_data: AlertAction, bot_user: BotUser
) -> None:
    """Переписка вокруг сообщения. ctx — кнопки ранее разосланных алертов;
    ctxm — из разметки. Разнится только подпись под выпиской."""
    if not has_perm(bot_user, Perm.REPORT_CHAT):
        await query.answer("Недостаточно прав", show_alert=True)
        return

    await query.answer("Собираю переписку…")
    tail = _TAILS.get(callback_data.action, "")
    await _render(
        query, callback_data, before=BEFORE, after=AFTER, edit=False, tail=tail
    )


@router.callback_query(AlertAction.filter(F.action == "ctxx"))
async def on_context_close(query: CallbackQuery, bot_user: BotUser) -> None:
    try:
        await query.message.delete()
    except Exception:  # noqa: BLE001 — сообщение старше 48 часов удалить нельзя
        await query.message.edit_text("<i>Переписка закрыта.</i>", parse_mode="HTML")
    await query.answer()


@router.callback_query(AlertAction.filter(F.action.in_({"ctxb", "ctxf"})))
async def on_context_page(
    query: CallbackQuery, callback_data: AlertAction, bot_user: BotUser
) -> None:
    """Листание выписки назад (ctxb) и вперёд (ctxf).

    На callback отвечает `_render`: ранний пустой answer съел бы «Дальше сообщений нет».
    """
    if not has_perm(bot_user, Perm.REPORT_CHAT):
        await query.answer("Недостаточно прав", show_alert=True)
        return

    if callback_data.action == "ctxb":
        # Якорь — самое раннее показанное: берём то, что было ДО него.
        before, after = WINDOW, 1
    else:
        before, after = 1, WINDOW

    await _render(
        query,
        callback_data,
        before=before,
        after=after,
        edit=True,
        tail="Листайте кнопками ниже.",
    )


# ── «Снять нарушение»: решение руководителя из среза отчёта ─────


def _dismiss_buttons(dismissed: bool, chat_id: int, msg_id: int) -> list[tuple[str, str]]:
    if dismissed:
        return [
            (
                "↩︎ Вернуть нарушение",
                AlertAction(action="undis", chat_id=chat_id, msg_id=msg_id).pack(),
            )
        ]
    return [
        (
            "✔️ Снять нарушение",
            AlertAction(action="dis", chat_id=chat_id, msg_id=msg_id).pack(),
        )
    ]


@router.callback_query(AlertAction.filter(F.action == "ctxd"))
async def on_context_dismissable(
    query: CallbackQuery, callback_data: AlertAction, bot_user: BotUser
) -> None:
    """Переписка нарушения из среза отчёта с кнопкой решения: снимать
    нарушение можно только глядя на переписку."""
    # Решение — у кого есть сводный отчёт; остальные видят ту же выписку без кнопки.
    can_decide = has_perm(bot_user, Perm.REPORT_ALL_CHATS)
    if not can_decide and not has_perm(bot_user, Perm.REPORT_CHAT):
        await query.answer("Недостаточно прав", show_alert=True)
        return

    await query.answer("Собираю переписку…")
    dismissed = False
    if can_decide:
        async with session_scope() as session:
            dismissed = await is_dismissed(
                session, callback_data.chat_id, callback_data.msg_id
            )
    await _render(
        query,
        callback_data,
        before=BEFORE,
        after=AFTER,
        edit=False,
        tail=(
            (
                "▶️ — обращение. Если по переписке это не нарушение или ответ "
                "не требуется — снимите его кнопкой ниже: оно уйдёт из списков, "
                "очередей ожидания и счётчиков. Решение можно отменить."
            )
            if can_decide
            else "▶️ — обращение."
        ),
        extra_buttons=(
            _dismiss_buttons(dismissed, callback_data.chat_id, callback_data.msg_id)
            if can_decide
            else []
        ),
    )


@router.callback_query(AlertAction.filter(F.action.in_({"dis", "disno"})))
async def on_dismiss_confirm(
    query: CallbackQuery, callback_data: AlertAction, bot_user: BotUser
) -> None:
    """Переспросить перед снятием нарушения: решение меняет статистику.
    Возврат нарушения (undis) — без переспроса."""
    if not has_perm(bot_user, Perm.REPORT_ALL_CHATS):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    if callback_data.action == "dis":
        buttons = [
            (
                "✅ Да, снять",
                AlertAction(
                    action="disok", chat_id=callback_data.chat_id, msg_id=callback_data.msg_id
                ).pack(),
            ),
            (
                "✖️ Нет, оставить",
                AlertAction(
                    action="disno", chat_id=callback_data.chat_id, msg_id=callback_data.msg_id
                ).pack(),
            ),
        ]
        note = "Точно снимаем нарушение?"
    else:
        buttons = _dismiss_buttons(False, callback_data.chat_id, callback_data.msg_id)
        note = "Оставили как есть"
    try:
        await query.message.edit_reply_markup(
            reply_markup=_nav(callback_data.chat_id, [], buttons)
        )
    except TelegramBadRequest:
        pass
    await query.answer(note)


@router.callback_query(AlertAction.filter(F.action.in_({"disok", "undis"})))
async def on_dismiss_toggle(
    query: CallbackQuery, callback_data: AlertAction, bot_user: BotUser
) -> None:
    """Принять (после подтверждения) или отменить решение «снять нарушение»."""
    dismissing = callback_data.action == "disok"
    async with session_scope() as session:
        actor = await session.get(BotUser, bot_user.id)
        anchor = await session.get(Message, callback_data.msg_id)
        # Сообщение должно принадлежать чату из callback: иначе подделанная
        # кнопка сняла бы нарушение по чужому ключу.
        if actor is None or anchor is None or anchor.chat_id != callback_data.chat_id:
            await query.answer("Обращение не найдено", show_alert=True)
            return
        try:
            if dismissing:
                changed = await dismiss(
                    session, actor, callback_data.chat_id, callback_data.msg_id
                )
            else:
                changed = await restore(
                    session, actor, callback_data.chat_id, callback_data.msg_id
                )
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    if not changed:
        await query.answer("Уже сделано — ничего не изменилось")
    elif dismissing:
        await query.answer(
            "Снято: обращение больше не считается нарушением и ушло "
            "из списков и счётчиков. Отменить — кнопкой ниже.",
            show_alert=True,
        )
    else:
        await query.answer("Возвращено: обращение снова считается нарушением")

    # Перерисовываем только кнопки: переписка не изменилась.
    try:
        await query.message.edit_reply_markup(
            reply_markup=_nav(
                callback_data.chat_id,
                # После решения — только решение и закрытие, без листания.
                [],
                _dismiss_buttons(dismissing, callback_data.chat_id, callback_data.msg_id),
            )
        )
    except TelegramBadRequest:
        pass
