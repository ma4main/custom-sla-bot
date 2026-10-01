"""Раздел «Пользователи бота»: роли, заявки, приглашения, владение."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery
from aiogram.types import Message as TgMessage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from app.bot.callbacks import Nav, UserAction
from app.bot.handlers import AnsweredAlready
from app.bot.keyboards import (
    back_to,
    confirm,
    invite_created,
    invite_roles,
    user_card,
    users_list,
)
from app.db.base import session_scope
from app.db.models import BotRole, BotUser, BotUserState
from app.services.access import (
    ASSIGNABLE_ORDER,
    ROLE_HINTS,
    AccessError,
    Perm,
    approve_user,
    change_role,
    create_invite,
    disable_user,
    enable_user,
    has_perm,
    link_staff,
    promote_to_owner,
    role_label,
    state_label,
    transfer_ownership,
)
from app.services.staff import list_staff
from app.text import esc

router = Router(name="users")
# Только приватные диалоги: в группы бот не пишет и меню там не показывает.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

PER_PAGE = 8


def _roles_help() -> str:
    """Что даёт каждая роль — один текст для экранов заявки и приглашения."""
    return "\n".join(
        f"• <b>{role_label(role)}</b> — {ROLE_HINTS.get(role, '')}" for role in ASSIGNABLE_ORDER
    )


# Сотрудников на странице привязки: больше кнопок в столбик не помещается
# на экран телефона.
_STAFF_PAGE = 8


async def _render_list(query: CallbackQuery, page: int) -> None:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(BotUser.state, func.count(BotUser.id)).group_by(BotUser.state)
            )
        ).all()
        counts = {state: int(count) for state, count in rows}
        users = (
            await session.scalars(
                select(BotUser)
                .where(BotUser.state != BotUserState.PENDING)
                .order_by(BotUser.role, BotUser.id)
                .offset(page * PER_PAGE)
                .limit(PER_PAGE)
            )
        ).all()
        for user in users:
            session.expunge(user)

    active = counts.get(BotUserState.ACTIVE, 0)
    disabled = counts.get(BotUserState.DISABLED, 0)
    pending = counts.get(BotUserState.PENDING, 0)
    # В списке — все, кроме заявок: они живут за своей кнопкой.
    listed = active + disabled

    text = (
        "<b>Пользователи бота</b>\n\n"
        "Кто и с какими правами пользуется ботом.\n\n"
        f"Всего: <b>{active + disabled + pending}</b>\n"
        f"• активных: {active}\n"
        f"• отключённых: {disabled}\n"
        f"• ждут подтверждения: {pending}\n\n"
        "Ниже — активные и отключённые (отключённые помечены ⛔). "
        "Заявки открываются отдельной кнопкой.\n\n"
        "<i>Нажатие /start само по себе доступа не даёт — роль назначаете вы.</i>"
    )
    await query.message.edit_text(
        text, reply_markup=users_list(list(users), page, listed, pending), parse_mode="HTML"
    )
    await query.answer()


@router.callback_query(Nav.filter(F.to == "users"))
async def on_users(query: CallbackQuery, state: FSMContext, bot_user: BotUser) -> None:
    await state.clear()
    await _render_list(query, 0)


@router.callback_query(UserAction.filter(F.action == "list"))
async def on_list(query: CallbackQuery, callback_data: UserAction, bot_user: BotUser) -> None:
    await _render_list(query, callback_data.page)


@router.callback_query(UserAction.filter(F.action == "view"))
async def on_view(query: CallbackQuery, callback_data: UserAction, bot_user: BotUser) -> None:
    async with session_scope() as session:
        target = await session.get(BotUser, callback_data.user_id)
        if target is None:
            await query.answer("Не найден", show_alert=True)
            return
        session.expunge(target)

    name = target.display_name or target.username or str(target.tg_user_id)
    safe_name = esc(name)
    text = (
        f"<b>{safe_name}</b>\n\n"
        f"Роль: <b>{role_label(target.role)}</b> — {ROLE_HINTS.get(target.role, '')}\n"
        f"Состояние: {state_label(target.state)}\n"
        f"Уведомления в личные сообщения: <b>{'да' if target.notify_personal else 'нет'}</b>"
        + ("" if target.notify_personal else " — алерты и отчёты только в группе")
        + f"\nTelegram ID: <code>{target.tg_user_id}</code>"
    )
    await query.message.edit_text(
        text,
        reply_markup=user_card(
            target,
            callback_data.page,
            is_self=target.id == bot_user.id,
            can_transfer=has_perm(bot_user, Perm.OWNERSHIP_TRANSFER),
            can_manage=has_perm(bot_user, Perm.USER_MANAGE),
        ),
        parse_mode="HTML",
    )
    await query.answer()


@router.callback_query(UserAction.filter(F.action == "notify"))
async def on_notify_toggle(
    query: CallbackQuery, callback_data: UserAction, bot_user: BotUser
) -> None:
    """Тумблер «уведомления в личные сообщения» — себе или управляемому пользователю.

    Выключить можно только при включённой группе для алертов и рассылок
    (personal_mute_blocker).
    """
    from app.config import effective_notify_group_id
    from app.db.models import AuditLog
    from app.services.access import personal_mute_blocker
    from app.services.settings_store import get_section

    async with session_scope() as session:
        target = await session.get(BotUser, callback_data.user_id)
        if target is None:
            await query.answer("Не найден", show_alert=True)
            return
        if target.id != bot_user.id and not has_perm(bot_user, Perm.USER_MANAGE):
            await query.answer("Недостаточно прав", show_alert=True)
            return
        if target.notify_personal:
            reason = personal_mute_blocker(
                await get_section(session, "alerts"),
                await get_section(session, "digest"),
                effective_notify_group_id(),
            )
            if reason is not None:
                await query.answer(reason, show_alert=True)
                return
        target.notify_personal = not target.notify_personal
        session.add(
            AuditLog(
                actor_user_id=bot_user.id,
                action="user.notify_personal",
                object_type="bot_user",
                object_id=str(target.id),
                payload={"value": target.notify_personal},
            )
        )
        enabled = target.notify_personal

    await query.answer(
        "Личные сообщения включены: алерты и отчёты снова приходят сюда"
        if enabled
        else "Личные сообщения выключены: алерты и отчёты — только в группе уведомлений",
        show_alert=not enabled,
    )
    await on_view(AnsweredAlready(query), callback_data, bot_user)


@router.callback_query(UserAction.filter(F.action == "set_role"))
async def on_set_role(query: CallbackQuery, callback_data: UserAction, bot_user: BotUser) -> None:
    async with session_scope() as session:
        actor = await session.get(BotUser, bot_user.id)
        target = await session.get(BotUser, callback_data.user_id)
        if target is None or actor is None:
            await query.answer("Не найден", show_alert=True)
            return
        try:
            await change_role(session, actor, target, BotRole(callback_data.role))
        except (AccessError, ValueError) as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer(f"Новая роль: {role_label(BotRole(callback_data.role))}")
    await _render_list(AnsweredAlready(query), callback_data.page)


@router.callback_query(UserAction.filter(F.action == "disable"))
async def on_disable(query: CallbackQuery, callback_data: UserAction, bot_user: BotUser) -> None:
    async with session_scope() as session:
        actor = await session.get(BotUser, bot_user.id)
        target = await session.get(BotUser, callback_data.user_id)
        if target is None or actor is None:
            await query.answer("Не найден", show_alert=True)
            return
        try:
            await disable_user(session, actor, target)
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer("Пользователь отключён")
    await _render_list(AnsweredAlready(query), callback_data.page)


@router.callback_query(UserAction.filter(F.action == "enable"))
async def on_enable(query: CallbackQuery, callback_data: UserAction, bot_user: BotUser) -> None:
    async with session_scope() as session:
        actor = await session.get(BotUser, bot_user.id)
        target = await session.get(BotUser, callback_data.user_id)
        if target is None or actor is None:
            await query.answer("Не найден", show_alert=True)
            return
        try:
            await enable_user(session, actor, target)
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        role = target.role

    await query.answer(f"Пользователь снова включён — {role_label(role)}")
    await _render_list(AnsweredAlready(query), callback_data.page)


# ── Владение: совладелец и передача ───────────────────────────


@router.callback_query(UserAction.filter(F.action == "co_owner"))
async def on_co_owner_confirm(
    query: CallbackQuery, callback_data: UserAction, bot_user: BotUser
) -> None:
    async with session_scope() as session:
        target = await session.get(BotUser, callback_data.user_id)
        if target is None:
            await query.answer("Не найден", show_alert=True)
            return
        session.expunge(target)

    name = target.display_name or target.username or str(target.tg_user_id)
    safe_name = esc(name)
    text = (
        "<b>Совладелец</b>\n\n"
        f"<b>{safe_name}</b> получит роль владельца. Ваша роль сохранится.\n\n"
        "Совладельцы равноправны: каждый управляет пользователями и настройками "
        "и может понизить другого — пока в системе остаётся хотя бы один владелец."
    )
    await query.message.edit_text(
        text,
        reply_markup=confirm(
            UserAction(action="co_owner_do", user_id=target.id, page=callback_data.page).pack(),
            UserAction(action="list", page=callback_data.page).pack(),
            label="👑 Сделать совладельцем",
        ),
        parse_mode="HTML",
    )
    await query.answer()


@router.callback_query(UserAction.filter(F.action == "co_owner_do"))
async def on_co_owner(query: CallbackQuery, callback_data: UserAction, bot_user: BotUser) -> None:
    async with session_scope() as session:
        actor = await session.get(BotUser, bot_user.id)
        target = await session.get(BotUser, callback_data.user_id)
        if target is None or actor is None:
            await query.answer("Не найден", show_alert=True)
            return
        try:
            await promote_to_owner(session, actor, target)
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer("Теперь совладелец", show_alert=True)
    await _render_list(AnsweredAlready(query), callback_data.page)


@router.callback_query(UserAction.filter(F.action == "transfer"))
async def on_transfer_confirm(
    query: CallbackQuery, callback_data: UserAction, bot_user: BotUser
) -> None:
    """Передача владения требует подтверждения: действие необратимо в один клик."""
    async with session_scope() as session:
        target = await session.get(BotUser, callback_data.user_id)
        if target is None:
            await query.answer("Не найден", show_alert=True)
            return
        session.expunge(target)

    name = target.display_name or target.username or str(target.tg_user_id)
    safe_name = esc(name)
    text = (
        "<b>Передача владения</b>\n\n"
        f"Роль владельца перейдёт к: <b>{safe_name}</b>\n"
        "Вы станете администратором.\n\n"
        "Если хотите сохранить свою роль — используйте «Сделать совладельцем»."
    )
    await query.message.edit_text(
        text,
        reply_markup=confirm(
            UserAction(action="transfer_do", user_id=target.id, page=callback_data.page).pack(),
            UserAction(action="list", page=callback_data.page).pack(),
            label="👑 Передать владение",
        ),
        parse_mode="HTML",
    )
    await query.answer()


@router.callback_query(UserAction.filter(F.action == "transfer_do"))
async def on_transfer(query: CallbackQuery, callback_data: UserAction, bot_user: BotUser) -> None:
    async with session_scope() as session:
        actor = await session.get(BotUser, bot_user.id)
        target = await session.get(BotUser, callback_data.user_id)
        if target is None or actor is None:
            await query.answer("Не найден", show_alert=True)
            return
        try:
            await transfer_ownership(session, actor, target)
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer("Владение передано", show_alert=True)
    await _render_list(AnsweredAlready(query), callback_data.page)


# ── Заявки и приглашения ──────────────────────────────────────


@router.callback_query(UserAction.filter(F.action == "pending"))
async def on_pending(query: CallbackQuery, bot_user: BotUser) -> None:
    async with session_scope() as session:
        users = (
            await session.scalars(
                select(BotUser).where(BotUser.state == BotUserState.PENDING).limit(20)
            )
        ).all()
        for user in users:
            session.expunge(user)

    if not users:
        text = "<b>Заявки на доступ</b>\n\nНовых заявок нет."
        await query.message.edit_text(text, reply_markup=back_to("users"), parse_mode="HTML")
        await query.answer()
        return

    builder = InlineKeyboardBuilder()
    lines = []
    for user in users:
        name = user.display_name or user.username or str(user.tg_user_id)
        lines.append(f"• {esc(name)} (<code>{user.tg_user_id}</code>)")
        for role in ASSIGNABLE_ORDER:
            builder.button(
                text=f"{name[:18]} → {role_label(role)}",
                callback_data=UserAction(
                    action="approve", user_id=user.id, role=role.value
                ).pack(),
            )
    builder.button(text="‹ Назад", callback_data=Nav(to="users").pack())
    builder.adjust(1)

    text = (
        "<b>Заявки на доступ</b>\n\n"
        + "\n".join(lines)
        + "\n\nВыберите роль для человека:\n"
        + _roles_help()
        + "\n\n<i>Владельца в этом списке нет намеренно: сначала выдайте роль, "
        "затем в карточке — «👑 Сделать совладельцем». Так нельзя отдать полный "
        "доступ одним случайным нажатием.</i>"
    )
    await query.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await query.answer()


@router.callback_query(UserAction.filter(F.action == "approve"))
async def on_approve(query: CallbackQuery, callback_data: UserAction, bot_user: BotUser) -> None:
    async with session_scope() as session:
        actor = await session.get(BotUser, bot_user.id)
        target = await session.get(BotUser, callback_data.user_id)
        if target is None or actor is None:
            await query.answer("Не найден", show_alert=True)
            return
        try:
            await approve_user(session, actor, target, BotRole(callback_data.role))
        except (AccessError, ValueError) as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer(f"Подтверждён — {role_label(BotRole(callback_data.role))}")

    # После выдачи роли «Сотрудник» сразу открывается привязка к справочнику:
    # без неё не работают «Мои показатели» и персональные алерты.
    if BotRole(callback_data.role) is BotRole.MANAGER:
        # Callback уже отвечен строкой выше — повторный answer Telegram
        # отвергает, поэтому рендеру передаётся обёртка с немым answer.
        await _render_link_pick(
            AnsweredAlready(query), user_id=callback_data.user_id, users_page=0
        )
        return
    await _render_list(AnsweredAlready(query), 0)


@router.callback_query(UserAction.filter(F.action == "invite_menu"))
async def on_invite_menu(query: CallbackQuery, bot_user: BotUser) -> None:
    text = (
        "<b>Приглашение</b>\n\n"
        "Одноразовая ссылка с заранее выбранной ролью. Человек переходит по ней "
        "и сразу получает доступ — подтверждать заявку отдельно не нужно.\n\n"
        "Кого приглашаем:\n" + _roles_help() + "\n\n"
        "<i>Роль владельца приглашением не выдаётся — только через «Сделать "
        "совладельцем» или передачу владения.</i>"
    )
    await query.message.edit_text(text, reply_markup=invite_roles(), parse_mode="HTML")
    await query.answer()


@router.callback_query(UserAction.filter(F.action == "invite_create"))
async def on_invite_create(
    query: CallbackQuery, callback_data: UserAction, bot_user: BotUser
) -> None:
    async with session_scope() as session:
        actor = await session.get(BotUser, bot_user.id)
        if actor is None:
            await query.answer("Ваша учётная запись не найдена — откройте /menu заново", show_alert=True)
            return
        try:
            # Код возвращается отдельно от записи: в базе лежит только хеш,
            # и показать ссылку второй раз будет уже неоткуда.
            _, code = await create_invite(session, actor, BotRole(callback_data.role))
        except (AccessError, ValueError) as exc:
            await query.answer(str(exc), show_alert=True)
            return

    me = await query.bot.get_me()
    link = f"https://t.me/{me.username}?start={code}"
    # Ссылка в <code>: Telegram не делает её кликабельной (нажатие погасило бы
    # одноразовое приглашение) и не рисует предпросмотр.
    text = (
        f"<b>Приглашение: {role_label(BotRole(callback_data.role))}</b>\n\n"
        f"<code>{link}</code>\n\n"
        "Нажмите «📋 Скопировать ссылку» и отправьте её человеку любым способом. "
        "Он перейдёт по ней и сразу получит доступ.\n\n"
        "<i>Ссылка одноразовая, действует 48 часов и показывается один раз: "
        "в базе хранится только её отпечаток. Потеряли или не успели — "
        "выпустите новую. Сами по ней не переходите.</i>"
    )
    await query.message.edit_text(text, reply_markup=invite_created(link), parse_mode="HTML")
    await query.answer()


# ── Привязка учётной записи к сотруднику из справочника ──────


class StaffLinkForm(StatesGroup):
    waiting_query = State()


async def _render_link_pick(
    query: CallbackQuery,
    *,
    user_id: int,
    users_page: int,
    staff_page: int = 0,
    search: str | None = None,
    edit: bool = True,
) -> None:
    """Выбор сотрудника списком или результатом поиска — оба пути ведут в `link_do`."""
    async with session_scope() as session:
        target = await session.get(BotUser, user_id)
        if target is None:
            await query.answer("Не найден", show_alert=True)
            return
        people, total = await list_staff(
            session,
            offset=staff_page * _STAFF_PAGE,
            limit=_STAFF_PAGE,
            active_only=True,
            query=search,
        )
        for person in people:
            session.expunge(person)
        session.expunge(target)

    callback_data = UserAction(action="link", user_id=user_id, page=users_page)
    builder = InlineKeyboardBuilder()
    for person in people:
        mark = "• " if target.staff_id == person.id else ""
        builder.button(
            text=f"{mark}{person.full_name}"[:60],
            callback_data=UserAction(
                action="link_do", user_id=target.id, page=callback_data.page, role=str(person.id)
            ).pack(),
        )

    # По результатам поиска не листаем: строка запроса живёт в FSM, а не в callback.
    pages = max(1, (total + _STAFF_PAGE - 1) // _STAFF_PAGE)
    if search is None and pages > 1:
        if staff_page > 0:
            builder.button(
                text="‹ Раньше",
                callback_data=UserAction(
                    action="link",
                    user_id=target.id,
                    page=callback_data.page,
                    role=f"p{staff_page - 1}",
                ).pack(),
            )
        if staff_page + 1 < pages:
            builder.button(
                text="Дальше ›",
                callback_data=UserAction(
                    action="link",
                    user_id=target.id,
                    page=callback_data.page,
                    role=f"p{staff_page + 1}",
                ).pack(),
            )

    if search is None:
        builder.button(
            text="🔎 Найти по имени",
            callback_data=UserAction(
                action="link_find", user_id=target.id, page=callback_data.page
            ).pack(),
        )
    else:
        builder.button(
            text="↩︎ Весь список",
            callback_data=UserAction(
                action="link", user_id=target.id, page=callback_data.page
            ).pack(),
        )

    if target.staff_id is not None:
        builder.button(
            text="✖️ Снять привязку",
            callback_data=UserAction(
                action="link_do", user_id=target.id, page=callback_data.page, role="0"
            ).pack(),
        )
    else:
        # Новичка может не быть в справочнике: запись появится с его первым сообщением.
        builder.button(
            text="⏭ Пока без привязки",
            callback_data=UserAction(
                action="link_skip", user_id=target.id, page=callback_data.page
            ).pack(),
        )
    builder.button(
        text="‹ Назад",
        callback_data=UserAction(action="view", user_id=target.id, page=callback_data.page).pack(),
    )
    builder.adjust(1)

    name = esc(target.display_name or target.username or str(target.tg_user_id))
    text = (
        f"<b>Привязка к сотруднику</b>\n\n"
        f"Кому из справочника соответствует <b>{name}</b>?\n\n"
        "<i>От этого зависят «Мои показатели» и личные алерты: отчёт "
        "считается по сообщениям сотрудника, а не по учётной записи в боте.</i>\n\n"
        "<i>Новичка в списке может ещё не быть — справочник наполняется "
        "сам, когда человек начинает писать в чатах. Тогда оставьте пока "
        "без привязки и вернитесь позже.</i>"
    )
    if search is not None:
        text += f"\n\n<i>Поиск: «{esc(search)}» — найдено {total}.</i>"
        if total > _STAFF_PAGE:
            text += f" <i>Показаны первые {_STAFF_PAGE}, уточните запрос.</i>"
        if not people:
            text += "\n<i>Никто не подошёл. Проверьте написание или откройте весь список.</i>"
    else:
        if pages > 1:
            text += f"\n\n<i>Страница {staff_page + 1} из {pages}, всего активных: {total}.</i>"
        if not people:
            text += "\n\n<i>Справочник пуст — сначала заведите сотрудников.</i>"

    markup = builder.as_markup()
    if edit:
        await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        # После ввода поиска редактировать нечего: последнее сообщение
        # в диалоге — текст человека, а не карточка бота.
        await query.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await query.answer()


@router.callback_query(UserAction.filter(F.action == "link"))
async def on_link_pick(query: CallbackQuery, callback_data: UserAction, bot_user: BotUser) -> None:
    """Выбрать, кому из справочника соответствует учётка: персональные отчёты
    считаются по Staff, а не по BotUser."""
    # Страница справочника едет в role как «p<N>»: page занято списком
    # пользователей, а новое поле сломало бы кнопки в старых сообщениях.
    raw_page = callback_data.role or ""
    staff_page = int(raw_page[1:]) if raw_page.startswith("p") and raw_page[1:].isdigit() else 0
    await _render_link_pick(
        query,
        user_id=callback_data.user_id,
        users_page=callback_data.page,
        staff_page=staff_page,
    )


@router.callback_query(UserAction.filter(F.action == "link_skip"))
async def on_link_skip(
    query: CallbackQuery, callback_data: UserAction, bot_user: BotUser
) -> None:
    """Осознанно оставить учётку без привязки — с пояснением, что дальше."""
    await query.answer(
        "Оставлено без привязки. «Мои показатели» и личные алерты заработают "
        "после неё: карточка пользователя → «Привязать к сотруднику». "
        "Новичок появится в справочнике сам, когда начнёт писать в чатах.",
        show_alert=True,
    )
    # Callback уже отвечен всплывашкой — карточке отвечать больше нельзя.
    await on_view(AnsweredAlready(query), callback_data, bot_user)


@router.callback_query(UserAction.filter(F.action == "link_find"))
async def on_link_find(
    query: CallbackQuery, callback_data: UserAction, state: FSMContext, bot_user: BotUser
) -> None:
    await state.set_state(StaffLinkForm.waiting_query)
    await state.update_data(user_id=callback_data.user_id, users_page=callback_data.page)
    await query.message.edit_text(
        "<b>Поиск по справочнику</b>\n\n"
        "Отправьте часть имени или фамилии сообщением — например "
        "<code>соколова</code>.\n\n"
        "<i>Регистр и «ё» не важны.</i>",
        reply_markup=back_to("users"),
        parse_mode="HTML",
    )
    await query.answer()


@router.message(StaffLinkForm.waiting_query)
async def on_link_query(message: TgMessage, state: FSMContext, bot_user: BotUser) -> None:
    data = await state.get_data()
    user_id = int(data.get("user_id") or 0)
    users_page = int(data.get("users_page") or 0)
    needle = (message.text or "").strip()

    if not needle:
        await message.answer("Пустой запрос — отправьте часть имени или вернитесь кнопкой «Назад».")
        return

    await state.clear()
    await _render_link_pick(
        AnsweredAlready(message=message),
        user_id=user_id,
        users_page=users_page,
        search=needle,
        edit=False,
    )


@router.callback_query(UserAction.filter(F.action == "link_do"))
async def on_link_do(query: CallbackQuery, callback_data: UserAction, bot_user: BotUser) -> None:
    # Разбор — до всего остального и с проверкой: role в callback может быть подделан.
    raw = (callback_data.role or "0").strip()
    if not raw.isdigit():
        await query.answer("Не понял, к кому привязывать", show_alert=True)
        return
    staff_id = int(raw)

    async with session_scope() as session:
        actor = await session.get(BotUser, bot_user.id)
        target = await session.get(BotUser, callback_data.user_id)
        if target is None or actor is None:
            await query.answer("Не найден", show_alert=True)
            return
        try:
            await link_staff(session, actor, target, staff_id or None)
        except (AccessError, ValueError) as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer("Привязка обновлена" if staff_id else "Привязка снята")
    await on_view(AnsweredAlready(query), callback_data, bot_user)
