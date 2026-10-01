"""Карточка «Кто это?» и список участников выписки.

Вход — только из выписки, где возник вопрос; неопределённого участника
размечают в один клик.
"""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.bot.callbacks import AlertAction, MarkupAction, SenderAction, StaffAction
from app.bot.handlers import AnsweredAlready
from app.db.base import session_scope
from app.db.models import (
    BotUser,
    BusinessSide,
    Chat,
    Message,
    SenderRuleKind,
    SenderRuleSide,
    Staff,
)
from app.services.access import AccessError, Perm, has_perm
from app.services.sender_rules import (
    RULED_ACTOR_KINDS,
    auth_hint,
    clear_rule,
    display_for,
    explain_side,
    kind_for,
    last_message,
    load_rule,
    scope_chat_id,
    sender_chats,
    set_rule,
    window_participants,
)
from app.services.settings_store import get_section
from app.services.staff_roles import (
    ROLE_MANAGER,
    ROLE_SPECIALIST,
    ROLE_UNDECIDED,
)
from app.services.transcript import calendar_tz, fmt_when, snippet
from app.text import esc

router = Router(name="sender_ui")
# Только приватные диалоги: в группы бот не пишет и меню там не показывает.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

# Экран роли — отдельное действие, а не поле: см. комментарий к SenderAction.
ROLE_ACTIONS = {"rsp": ROLE_SPECIALIST, "rmg": ROLE_MANAGER, "rnd": ROLE_UNDECIDED}
ROLE_BUTTONS = {
    ROLE_SPECIALIST: "🎓 Специалисты",
    ROLE_MANAGER: "🤝 Менеджеры",
    ROLE_UNDECIDED: "❓ Роль не определена",
}
# Сколько людей показывать в одной роли — столько же, сколько в очереди подписей.
ROLE_PAGE = 20

SIDE_WORDS = {
    BusinessSide.CLIENT: "🔵 клиент",
    BusinessSide.COMPANY: "🟢 компания",
    BusinessSide.INTEGRATOR_SYSTEM: "⚙️ система (человека за ней нет)",
    BusinessSide.UNKNOWN: "⚪ не определено",
}


def _role_of(person: Staff) -> str:
    return person.role if person.role in (ROLE_SPECIALIST, ROLE_MANAGER) else ROLE_UNDECIDED


async def _kind_of(session, key: int, chat_id: int | None) -> SenderRuleKind | None:
    """Вид отправителя по его сообщениям. В кнопку не кладётся: это свойство
    отправителя, а не решения."""
    query = (
        select(Message.transport_actor_kind)
        .where(Message.tg_user_id == key)
        .where(Message.transport_actor_kind.in_(RULED_ACTOR_KINDS))
    )
    if chat_id:
        query = query.where(Message.chat_id == chat_id)
    actor = await session.scalar(query.limit(1))
    if actor is None:
        # Правило может существовать и без сообщений: отправитель молчал,
        # а решение по нему уже приняли.
        rule_kind = None
        for candidate in (SenderRuleKind.ANONYMOUS_ADMIN, SenderRuleKind.BOT, SenderRuleKind.TG_USER):
            if await load_rule(session, candidate, key, chat_id):
                rule_kind = candidate
                break
        return rule_kind
    return kind_for(actor, key)


async def _render_card(
    query: CallbackQuery,
    bot_user: BotUser,
    *,
    key: int,
    chat_id: int,
    role: str | None,
    window: tuple[int, int],
) -> None:
    can_assign = has_perm(bot_user, Perm.ATTRIBUTION_ASSIGN)

    async with session_scope() as session:
        kind = await _kind_of(session, key, chat_id or None)
        if kind is None:
            await query.answer("Отправитель не найден", show_alert=True)
            return
        scoped = scope_chat_id(kind, chat_id or None)
        display = await display_for(session, kind, key, scoped or (chat_id or None))
        rule, side, reason = await explain_side(session, kind, key, scoped)
        has_rule = rule is not None
        author = None
        if rule is not None and rule.staff_id is not None:
            author = await session.scalar(
                select(Staff.full_name).where(Staff.id == rule.staff_id)
            )
        chats = await sender_chats(session, key, scoped)
        example = await last_message(session, key, scoped)
        example_chat = (
            await session.get(Chat, example.chat_id) if example is not None else None
        )
        excerpt = snippet(example, limit=300) if example is not None else ""
        calendar_cfg = await get_section(session, "work_calendar")

        hint = None
        if kind is SenderRuleKind.ANONYMOUS_ADMIN and scoped:
            found = await auth_hint(session, scoped)
            if found is not None:
                person, seen_at = found
                hint = (person.id, person.full_name, seen_at)

        people = (
            await session.scalars(
                select(Staff).where(Staff.active.is_(True)).order_by(Staff.full_name)
            )
        ).all()
        for person in people:
            session.expunge(person)

    by_role: dict[str, list[Staff]] = {}
    for person in people:
        by_role.setdefault(_role_of(person), []).append(person)

    def button(action: str, **extra) -> str:
        return SenderAction(
            action=action,
            key=key,
            chat_id=chat_id,
            msg_id=window[0],
            to_id=window[1],
            **extra,
        ).pack()

    # «От имени группы» без чата решать нечем: учётка одна на весь Telegram,
    # и правило без чата приписало бы решение всем группам разом.
    can_assign = can_assign and not (
        kind is SenderRuleKind.ANONYMOUS_ADMIN and not scoped
    )

    builder = InlineKeyboardBuilder()
    if role is not None:
        for person in by_role.get(role, [])[:ROLE_PAGE]:
            builder.button(text=person.full_name[:60], callback_data=button("bind", staff_id=person.id))
        builder.button(text="‹ К ролям", callback_data=button("card"))
    elif can_assign:
        if hint is not None:
            # Подсказка в одно нажатие: при подключении чата Битрикс называет имя
            # учётки. Правило создаёт только нажатие человека, а не разбор текста.
            builder.button(
                text=f"✅ Да, это {hint[1][:40]}",
                callback_data=button("auth", staff_id=hint[0]),
            )
        if people:
            builder.button(text="👤 Сотрудник ›", callback_data=button("roles"))
        builder.button(text="🔵 Клиент", callback_data=button("cli"))
        builder.button(text="⚙️ Система / бот", callback_data=button("sys"))
        if has_rule:
            builder.button(text="↩️ Сбросить решение", callback_data=button("del"))

    if role is None:
        if example is not None:
            builder.button(
                text="💬 Показать переписку",
                callback_data=AlertAction(
                    action="ctxm", chat_id=example.chat_id, msg_id=example.id
                ).pack(),
            )
        builder.button(text="‹ Назад", callback_data=_back(window))
    builder.adjust(1)

    lines = ["<b>Кто это?</b>\n", f"<b>{esc(display)}</b>"]
    if kind is not SenderRuleKind.ANONYMOUS_ADMIN:
        lines.append(f"Telegram ID: <code>{key}</code>")
    if hint is not None:
        when = fmt_when(hint[2], calendar_tz(calendar_cfg), datetime.now(timezone.utc))
        lines.append(
            f"\n<i>Похоже, это {esc(hint[1])} — по авторизации в Битриксе ({when}).</i>"
        )
    lines.append(f"\nСейчас: <b>{SIDE_WORDS.get(side, side.value)}</b> — {reason}.")
    if author:
        lines.append(f"Автор сообщений: <b>{esc(author)}</b>")

    if chats:
        lines.append(f"\nВсего сообщений: <b>{sum(count for _, count in chats)}</b>")
        for title, count in chats[:5]:
            lines.append(f"• {esc(title)} — {count}")
        if len(chats) > 5:
            lines.append(f"• …и ещё чатов: {len(chats) - 5}")
    else:
        lines.append("\n<i>Сообщений от этого отправителя пока нет.</i>")

    if example is not None:
        when = fmt_when(
            example.sent_at, calendar_tz(calendar_cfg), datetime.now(timezone.utc)
        )
        lines.append(
            f"\nПоследнее — «{esc((example_chat.title if example_chat else None) or '?')}», {when}:"
        )
        lines.append(f"<blockquote>{excerpt}</blockquote>")

    if role is not None:
        lines.append(
            f"<b>{ROLE_BUTTONS[role]}</b> — выберите человека: его имя встанет "
            "автором всех сообщений этого отправителя."
        )
    elif not can_assign:
        lines.append(
            "\n<i>Только просмотр: менять разметку может владелец или администратор.</i>"
        )
    else:
        lines.append(
            "\n<i>Решение относится к ОТПРАВИТЕЛЮ и действует на всю его "
            "переписку — прошлую и будущую. Сторона сообщений пересчитается "
            "сразу, отчёты и алерты подхватят её при следующем проходе — "
            "обычно в течение минуты.</i>"
        )
        if kind is SenderRuleKind.ANONYMOUS_ADMIN:
            lines.append(
                "\n<i>Писали «от имени группы»: имени Telegram в таком сообщении "
                "не передаёт вовсе, поэтому решение действует в этом чате — "
                "в другом за той же учётной записью стоит другой человек.</i>"
            )

    await query.message.edit_text(
        "\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await query.answer()


def _back(window: tuple[int, int]) -> str:
    if window[0] and window[1]:
        return SenderAction(action="parts", msg_id=window[0], to_id=window[1]).pack()
    return StaffAction(action="unresolved").pack()


@router.callback_query(SenderAction.filter(F.action == "card"))
async def on_card(
    query: CallbackQuery, callback_data: SenderAction, bot_user: BotUser
) -> None:
    await _render_card(
        query,
        bot_user,
        key=callback_data.key,
        chat_id=callback_data.chat_id,
        role=None,
        window=(callback_data.msg_id, callback_data.to_id),
    )


@router.callback_query(SenderAction.filter(F.action == "roles"))
async def on_roles(
    query: CallbackQuery, callback_data: SenderAction, bot_user: BotUser
) -> None:
    if not has_perm(bot_user, Perm.ATTRIBUTION_ASSIGN):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    async with session_scope() as session:
        people = (
            await session.scalars(
                select(Staff).where(Staff.active.is_(True)).order_by(Staff.full_name)
            )
        ).all()
        for person in people:
            session.expunge(person)

    by_role: dict[str, int] = {}
    for person in people:
        by_role[_role_of(person)] = by_role.get(_role_of(person), 0) + 1

    builder = InlineKeyboardBuilder()
    for code, title in ROLE_BUTTONS.items():
        if by_role.get(code):
            action = next(key for key, value in ROLE_ACTIONS.items() if value == code)
            builder.button(
                text=f"{title} — {by_role[code]}",
                callback_data=SenderAction(
                    action=action,
                    key=callback_data.key,
                    chat_id=callback_data.chat_id,
                    msg_id=callback_data.msg_id,
                    to_id=callback_data.to_id,
                ).pack(),
            )
    builder.button(
        text="‹ Назад",
        callback_data=SenderAction(
            action="card",
            key=callback_data.key,
            chat_id=callback_data.chat_id,
            msg_id=callback_data.msg_id,
            to_id=callback_data.to_id,
        ).pack(),
    )
    builder.adjust(1)
    await query.message.edit_text(
        "<b>Кто это из сотрудников?</b>\n\n"
        "Выберите роль, а в ней — человека. Его имя встанет автором всех "
        "сообщений этого отправителя, а сами сообщения — на сторону компании.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await query.answer()


@router.callback_query(SenderAction.filter(F.action.in_(set(ROLE_ACTIONS))))
async def on_role_people(
    query: CallbackQuery, callback_data: SenderAction, bot_user: BotUser
) -> None:
    if not has_perm(bot_user, Perm.ATTRIBUTION_ASSIGN):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    await _render_card(
        query,
        bot_user,
        key=callback_data.key,
        chat_id=callback_data.chat_id,
        role=ROLE_ACTIONS[callback_data.action],
        window=(callback_data.msg_id, callback_data.to_id),
    )


# Кнопка → сторона правила. Сторона в действии, а не отдельным полем:
# новое поле сломало бы уже отправленные кнопки.
_SIDE_BY_ACTION = {
    "bind": SenderRuleSide.COMPANY,
    "auth": SenderRuleSide.COMPANY,
    "cli": SenderRuleSide.CLIENT,
    "sys": SenderRuleSide.SYSTEM,
}


@router.callback_query(SenderAction.filter(F.action.in_(set(_SIDE_BY_ACTION))))
async def on_decide(
    query: CallbackQuery, callback_data: SenderAction, bot_user: BotUser
) -> None:
    """Принять решение: правило записывается и применяется задним числом."""
    if not has_perm(bot_user, Perm.ATTRIBUTION_ASSIGN):
        await query.answer("Недостаточно прав", show_alert=True)
        return

    side = _SIDE_BY_ACTION[callback_data.action]
    async with session_scope() as session:
        actor = await session.get(BotUser, bot_user.id)
        kind = await _kind_of(session, callback_data.key, callback_data.chat_id or None)
        if actor is None or kind is None:
            await query.answer("Отправитель не найден", show_alert=True)
            return
        scoped = scope_chat_id(kind, callback_data.chat_id or None)
        if kind is SenderRuleKind.ANONYMOUS_ADMIN and scoped is None:
            await query.answer(
                "«От имени группы» размечается из переписки конкретного чата: "
                "откройте её и нажмите «👥 Участники».",
                show_alert=True,
            )
            return
        staff_id = callback_data.staff_id or None
        person = None
        if side is SenderRuleSide.COMPANY:
            if staff_id is None:
                await query.answer("Выберите человека", show_alert=True)
                return
            person = await session.scalar(select(Staff.full_name).where(Staff.id == staff_id))
            if person is None:
                await query.answer("Сотрудник не найден", show_alert=True)
                return
        display = await display_for(session, kind, callback_data.key, scoped or (callback_data.chat_id or None))
        try:
            _, stats = await set_rule(
                session,
                actor,
                kind=kind,
                key=callback_data.key,
                chat_id=scoped,
                side=side,
                staff_id=staff_id,
                display=display,
            )
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    what = person if person else ("клиент" if side is SenderRuleSide.CLIENT else "система / бот")
    await query.answer(
        f"Запомнили: {what}. Пересчитано сообщений: {stats['messages']}"
        + (f", сменили сторону: {stats['changed']}" if stats["changed"] else "")
        + ". Обращения и отчёты обновятся при следующем проходе — обычно в течение минуты.",
        show_alert=True,
    )
    await _render_card(
        AnsweredAlready(query),
        bot_user,
        key=callback_data.key,
        chat_id=callback_data.chat_id,
        role=None,
        window=(callback_data.msg_id, callback_data.to_id),
    )


@router.callback_query(SenderAction.filter(F.action == "del"))
async def on_reset(
    query: CallbackQuery, callback_data: SenderAction, bot_user: BotUser
) -> None:
    """Сбросить решение — отправитель снова определяется правилами кода."""
    if not has_perm(bot_user, Perm.ATTRIBUTION_ASSIGN):
        await query.answer("Недостаточно прав", show_alert=True)
        return

    async with session_scope() as session:
        actor = await session.get(BotUser, bot_user.id)
        kind = await _kind_of(session, callback_data.key, callback_data.chat_id or None)
        if actor is None or kind is None:
            await query.answer("Отправитель не найден", show_alert=True)
            return
        rule = await load_rule(
            session, kind, callback_data.key, scope_chat_id(kind, callback_data.chat_id or None)
        )
        if rule is None:
            await query.answer("Решения и не было", show_alert=True)
            return
        try:
            stats = await clear_rule(session, actor, rule)
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer(
        f"Решение снято, сторону снова выбирает бот. Пересчитано сообщений: "
        f"{stats['messages']}.",
        show_alert=True,
    )
    await _render_card(
        AnsweredAlready(query),
        bot_user,
        key=callback_data.key,
        chat_id=callback_data.chat_id,
        role=None,
        window=(callback_data.msg_id, callback_data.to_id),
    )


@router.callback_query(SenderAction.filter(F.action == "parts"))
async def on_participants(
    query: CallbackQuery, callback_data: SenderAction, bot_user: BotUser
) -> None:
    """Кто писал в показанном окне выписки, включая клиентов: так исправляют
    и клиента, принятого за сотрудника, и сотрудника, пишущего напрямую."""
    async with session_scope() as session:
        anchor = await session.get(Message, callback_data.msg_id)
        if anchor is None:
            await query.answer("Переписка не найдена", show_alert=True)
            return
        chat = await session.get(Chat, anchor.chat_id)
        rows = await window_participants(
            session, anchor.chat_id, callback_data.msg_id, callback_data.to_id
        )

    builder = InlineKeyboardBuilder()
    for row in rows:
        if row["kind"] == "signature":
            # Подпись интегратора разбирается очередью подписей — ведём в его
            # карточку, а не заводим второй способ решать одно и то же.
            packed = MarkupAction(action="pick", msg_id=row["anchor"]).pack()
        else:
            packed = SenderAction(
                action="card",
                key=row["key"],
                chat_id=row["chat_id"] or 0,
                msg_id=callback_data.msg_id,
                to_id=callback_data.to_id,
            ).pack()
        builder.button(text=f"{row['label'][:50]} — {row['messages']}", callback_data=packed)
    builder.button(
        text="‹ К переписке",
        callback_data=AlertAction(
            action="ctxf", chat_id=anchor.chat_id, msg_id=callback_data.msg_id
        ).pack(),
    )
    builder.adjust(1)

    head = f"<b>👥 Участники — «{esc((chat.title if chat else None) or '?')}»</b>\n"
    if not rows:
        text = f"{head}\nВ показанном куске переписки никто не писал."
    else:
        text = (
            f"{head}\n"
            "Кто писал в показанном куске переписки и чем его считает бот.\n\n"
            "Нажмите на участника, чтобы поправить: сторона и автор его "
            "сообщений пересчитаются за всё время.\n\n"
            "<i>Сотрудники, подписанные Битриксом, сюда не попадают — их автор "
            "уже известен.</i>"
        )
    await query.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await query.answer()
