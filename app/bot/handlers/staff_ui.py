"""Раздел «Сотрудники»: справочник и ручная разметка нераспознанных."""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message as TgMessage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.text import esc
from app.bot.callbacks import AlertAction, MarkupAction, Nav, SenderAction, StaffAction
from app.bot.handlers import AnsweredAlready
from app.bot.keyboards import (
    back_to,
    staff_card,
    staff_group,
    staff_role_choice,
    staff_root,
)
from app.db.base import session_scope
from app.db.models import Attribution, BotUser, Chat, Message, Staff
from app.services.access import AccessError, Perm, has_perm
from app.services.attribution import (
    attribute_all,
    bind_raw_name,
    group_size,
    mark_not_staff,
    not_staff_group_size,
    not_staff_groups,
    restore_to_queue,
    unresolved_groups,
)
from app.services.staff import (
    StaffError,
    add_alias,
    create_staff,
    list_staff,
    set_active,
    unresolved_count,
)
from app.services.sender_rules import unruled_bot_senders
from app.services.settings_store import get_section
from app.services.staff_roles import (
    ROLE_LABELS,
    ROLE_MANAGER,
    ROLE_SPECIALIST,
    ROLE_UNDECIDED,
    role_line,
    set_manual_role,
)
from app.services.transcript import calendar_tz, fmt_when, snippet

router = Router(name="staff")
# Только приватные диалоги: в группы бот не пишет и меню там не показывает.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

PER_PAGE = 8


class StaffForm(StatesGroup):
    waiting_name = State()
    waiting_alias = State()


# Группа → (заголовок, пояснение на экране группы).
GROUPS = {
    ROLE_SPECIALIST: (
        "🎓 Специалисты",
        "Отвечают по существу на вопросы, переданные им менеджерами.",
    ),
    ROLE_MANAGER: (
        "🤝 Менеджеры",
        "Реагируют на обращение первыми и передают вопрос специалисту, "
        "если он не в их компетенции.",
    ),
    ROLE_UNDECIDED: (
        "❓ Роль пока не определена",
        "По переписке пока мало наблюдений, чтобы система решила сама "
        "(нужно два независимых случая передачи или ответа). Роль можно "
        "задать вручную в карточке человека — автоматика её не тронет.",
    ),
}


async def _render_root(query: CallbackQuery) -> None:
    async with session_scope() as session:
        total = (await list_staff(session, limit=1))[1]
        specialists = (await list_staff(session, limit=1, role=ROLE_SPECIALIST))[1]
        managers = (await list_staff(session, limit=1, role=ROLE_MANAGER))[1]
        undecided = (await list_staff(session, limit=1, role=ROLE_UNDECIDED))[1]
        active = (await list_staff(session, limit=1, active_only=True))[1]
        # На кнопке — число строк очереди (подписи плюс боты), а не сообщений.
        unresolved = len(await unresolved_groups(session, limit=100)) + len(
            await unruled_bot_senders(session, limit=100)
        )
        not_staff = await not_staff_groups(session, limit=100)

    lines = [
        "<b>👥 Сотрудники</b>\n",
        f"Справочник: <b>{total}</b>",
        f"• специалистов: {specialists}",
        f"• менеджеров: {managers}",
        f"• роль пока не определена: {undecided}",
    ]
    if total - active:
        lines.append(f"• больше не работают: {total - active} (в списках ⛔)")
    if not_staff:
        # За этими подписями человека нет, но сообщения в чатах есть,
        # и правила для них другие.
        messages = sum(count for _, count, _ in not_staff)
        lines.append(f"\nНе сотрудники: {len(not_staff)} — рассылки и боты ({messages} сообщ.)")
    lines.append(
        "\nРоль система определяет сама по переписке:\n"
        "🤝 кто передаёт вопрос дальше — менеджер;\n"
        "🎓 кто отвечает по существу после передачи — специалист.\n\n"
        "<i>В карточке человека роль можно задать вручную.</i>"
    )

    await query.message.edit_text(
        "\n".join(lines),
        reply_markup=staff_root(
            specialists=specialists,
            managers=managers,
            undecided=undecided,
            unresolved=unresolved,
            not_staff=len(not_staff),
        ),
        parse_mode="HTML",
    )
    await query.answer()


async def _render_group(query: CallbackQuery, page: int, role: str) -> None:
    async with session_scope() as session:
        people, total = await list_staff(
            session, offset=page * PER_PAGE, limit=PER_PAGE, role=role
        )
        for person in people:
            session.expunge(person)

    title, description = GROUPS[role]
    text = f"<b>{title}</b>\n\nВсего: {total}\n\n<i>{description}</i>"
    if not people:
        text += "\n\n<i>Здесь пока никого.</i>"

    await query.message.edit_text(
        text, reply_markup=staff_group(people, page, total, role), parse_mode="HTML"
    )
    await query.answer()


@router.callback_query(Nav.filter(F.to == "staff"))
async def on_staff(
    query: CallbackQuery, bot_user: BotUser, state: FSMContext | None = None
) -> None:
    # «Назад» из формы ведёт сюда — ожидание текста надо снять, иначе
    # следующее сообщение человека утечёт в брошенную форму.
    if state is not None:
        await state.clear()
    await _render_root(query)


@router.callback_query(StaffAction.filter(F.action == "list"))
async def on_list(query: CallbackQuery, callback_data: StaffAction, bot_user: BotUser) -> None:
    await _render_root(query)


@router.callback_query(StaffAction.filter(F.action == "list_s"))
async def on_list_specialists(
    query: CallbackQuery, callback_data: StaffAction, bot_user: BotUser
) -> None:
    await _render_group(query, callback_data.page, ROLE_SPECIALIST)


@router.callback_query(StaffAction.filter(F.action == "list_m"))
async def on_list_managers(
    query: CallbackQuery, callback_data: StaffAction, bot_user: BotUser
) -> None:
    await _render_group(query, callback_data.page, ROLE_MANAGER)


@router.callback_query(StaffAction.filter(F.action == "list_u"))
async def on_list_undecided(
    query: CallbackQuery, callback_data: StaffAction, bot_user: BotUser
) -> None:
    await _render_group(query, callback_data.page, ROLE_UNDECIDED)


async def _render_card(query: CallbackQuery, staff_id: int, page: int) -> None:
    async with session_scope() as session:
        person = await session.get(Staff, staff_id)
        if person is None:
            await query.answer("Не найден", show_alert=True)
            return
        session.expunge(person)

    aliases = ", ".join(row.alias for row in person.aliases) if person.aliases else "нет"
    lines = [
        f"<b>{esc(person.full_name)}</b>\n",
        f"Роль: {role_line(person)}",
        f"Статус: {'работает' if person.active else '⛔ больше не работает'}",
        f"Написания имени: {esc(aliases)}",
    ]
    if person.discriminator:
        lines.append(f"Уточнение (однофамильцы): {esc(person.discriminator)}")
    if person.tg_user_id:
        lines.append(f"Telegram ID: {person.tg_user_id}")
    lines.append(
        "\n<i>Написание имени — это подпись, которой интегратор подписывает "
        "сообщения человека. Добавьте вариант, если в чатах имя пишут иначе "
        "(другая фамилия, инициалы, опечатка): такие сообщения начнут "
        "узнаваться сами.</i>"
    )

    role_button = f"✏️ Роль: {ROLE_LABELS.get(person.role, 'пока не определена')}"
    await query.message.edit_text(
        "\n".join(lines), reply_markup=staff_card(person, page, role_button), parse_mode="HTML"
    )
    await query.answer()


@router.callback_query(StaffAction.filter(F.action == "view"))
async def on_view(
    query: CallbackQuery,
    callback_data: StaffAction,
    bot_user: BotUser,
    state: FSMContext | None = None,
) -> None:
    # Сюда же ведёт «Назад» из формы написания имени — снимаем ожидание.
    if state is not None:
        await state.clear()
    await _render_card(query, callback_data.staff_id, callback_data.page)


@router.callback_query(StaffAction.filter(F.action == "role"))
async def on_role(query: CallbackQuery, callback_data: StaffAction, bot_user: BotUser) -> None:
    async with session_scope() as session:
        person = await session.get(Staff, callback_data.staff_id)
        if person is None:
            await query.answer("Не найден", show_alert=True)
            return
        session.expunge(person)

    text = (
        f"<b>Роль: {esc(person.full_name)}</b>\n\n"
        f"Сейчас: <b>{role_line(person)}</b>\n\n"
        "🎓 <b>Специалист</b> — отвечает по существу на переданные вопросы.\n"
        "🤝 <b>Менеджер</b> — реагирует первым и передаёт специалисту.\n"
        "🤖 <b>Определять автоматически</b> — система смотрит на передачи: "
        "два независимых случая, и роль ставится сама.\n\n"
        "Выберите кнопкой."
    )
    await query.message.edit_text(
        text,
        reply_markup=staff_role_choice(person, callback_data.page),
        parse_mode="HTML",
    )
    await query.answer()


# Кнопка → роль. Значение едет в action, а не отдельным полем: новое поле
# в схеме callback сломало бы кнопки в уже отправленных сообщениях.
_ROLE_BY_ACTION = {"role_s": ROLE_SPECIALIST, "role_m": ROLE_MANAGER, "role_a": None}


@router.callback_query(StaffAction.filter(F.action.in_(set(_ROLE_BY_ACTION))))
async def on_role_set(
    query: CallbackQuery, callback_data: StaffAction, bot_user: BotUser
) -> None:
    async with session_scope() as session:
        person = await session.get(Staff, callback_data.staff_id)
        if person is None:
            await query.answer("Не найден", show_alert=True)
            return
        try:
            await set_manual_role(session, bot_user, person, _ROLE_BY_ACTION[callback_data.action])
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer("Роль сохранена")
    await _render_card(AnsweredAlready(query), callback_data.staff_id, callback_data.page)


@router.callback_query(StaffAction.filter(F.action.in_({"activate", "deactivate"})))
async def on_toggle(query: CallbackQuery, callback_data: StaffAction, bot_user: BotUser) -> None:
    activate = callback_data.action == "activate"
    async with session_scope() as session:
        person = await session.get(Staff, callback_data.staff_id)
        if person is None:
            await query.answer("Не найден", show_alert=True)
            return
        try:
            await set_active(session, bot_user, person, activate)
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer(
        "Вернули в работу: подпись снова узнаётся, роль пересчитывается."
        if activate
        else "Отмечено: больше не работает. Подпись в новых сообщениях "
        "узнаваться не будет, прошлые отчёты сохранятся.",
        show_alert=True,
    )
    await _render_card(AnsweredAlready(query), callback_data.staff_id, callback_data.page)


@router.callback_query(StaffAction.filter(F.action == "add"))
async def on_add(query: CallbackQuery, state: FSMContext, bot_user: BotUser) -> None:
    await state.set_state(StaffForm.waiting_name)
    await query.message.edit_text(
        "<b>Новый сотрудник</b>\n\n"
        "Отправьте имя в том виде, в каком его подставляет интегратор — "
        "«Имя Фамилия», например <code>Ирина Соколова</code>.\n\n"
        "Если такой человек уже есть, добавьте отличитель через запятую: "
        "<code>Иванов Иван, второй</code>",
        reply_markup=back_to("staff"),
        parse_mode="HTML",
    )
    await query.answer()


@router.message(StaffForm.waiting_name)
async def on_name(message: TgMessage, state: FSMContext, bot_user: BotUser) -> None:
    raw = (message.text or "").strip()
    name, _, discriminator = raw.partition(",")

    async with session_scope() as session:
        try:
            person = await create_staff(
                session, bot_user, name.strip(), (discriminator.strip() or None)
            )
        except (StaffError, AccessError) as exc:
            await message.answer(f"❌ {exc}")
            return
        created = person.full_name
        # Старые сообщения привязываются сразу, без отдельной кнопки.
        attribution = await attribute_all(session)

    await state.clear()
    await message.answer(
        f"✅ Добавлен: <b>{esc(created)}</b>\n"
        f"Старые сообщения переразмечены; нераспознанных осталось: "
        f"{attribution['unresolved']}.",
        parse_mode="HTML",
    )


@router.callback_query(StaffAction.filter(F.action == "alias"))
async def on_alias(
    query: CallbackQuery, callback_data: StaffAction, state: FSMContext, bot_user: BotUser
) -> None:
    await state.set_state(StaffForm.waiting_alias)
    await state.update_data(staff_id=callback_data.staff_id, page=callback_data.page)

    # «Назад» — в карточку человека, а не в корень раздела.
    builder = InlineKeyboardBuilder()
    builder.button(
        text="‹ Назад",
        callback_data=StaffAction(
            action="view", staff_id=callback_data.staff_id, page=callback_data.page
        ).pack(),
    )
    await query.message.edit_text(
        "<b>Написание имени</b>\n\n"
        "Отправьте сообщением то написание, которое встречается в чатах, — "
        "например <code>Соколова Ирина</code> или девичью фамилию.\n\n"
        "<i>Бот начнёт узнавать такие подписи сам и разметит уже накопленные "
        "сообщения.</i>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await query.answer()


@router.message(StaffForm.waiting_alias)
async def on_alias_value(message: TgMessage, state: FSMContext, bot_user: BotUser) -> None:
    data = await state.get_data()
    async with session_scope() as session:
        person = await session.get(Staff, data.get("staff_id", 0))
        if person is None:
            await message.answer("❌ Сотрудник не найден")
            await state.clear()
            return
        try:
            await add_alias(session, bot_user, person, message.text or "")
        except (StaffError, AccessError) as exc:
            await message.answer(f"❌ {exc}")
            return
        # Алиас подхватывает старые сообщения сразу, как и новый сотрудник.
        attribution = await attribute_all(session)

    await state.clear()
    await message.answer(
        "✅ Вариант написания добавлен\n"
        f"Старые сообщения переразмечены; нераспознанных осталось: "
        f"{attribution['unresolved']}."
    )


# ── Очередь разметки: нераспознанные авторы ───────────────────


async def _render_unresolved(query: CallbackQuery) -> None:
    """Очередь: подписи без сотрудника и чужие боты без решения.

    Людей здесь нет: их разбирают из переписки («Кто это?»). Кнопка подписи
    несёт якорное сообщение — решение применяется ко всей группе с этой подписью.
    """
    async with session_scope() as session:
        groups = await unresolved_groups(session, limit=PER_PAGE)
        total = await unresolved_count(session)
        # Считаем подписи, как кнопка в корне раздела.
        marked = len(await not_staff_groups(session, limit=100))
        bots = await unruled_bot_senders(session, limit=PER_PAGE)

    builder = InlineKeyboardBuilder()
    for raw_name, count, anchor in groups:
        label = raw_name or "сообщения без подписи"
        builder.button(
            text=f"{label[:40]} — {count}",
            callback_data=MarkupAction(action="pick", msg_id=anchor).pack(),
        )
    for bot in bots:
        builder.button(
            text=f"🤖 бот {bot['tg_user_id']} — {bot['messages']}",
            callback_data=SenderAction(action="card", key=bot["tg_user_id"]).pack(),
        )
    if marked:
        builder.button(
            text=f"🤖 Не сотрудники — {marked}",
            callback_data=MarkupAction(action="ignored").pack(),
        )
    builder.button(text="‹ Назад", callback_data=Nav(to="staff").pack())
    builder.adjust(1)

    head = (
        "<b>🔍 Не определили, кто это</b>\n\n"
        "Сюда попадает то, про что бот решить не может:\n\n"
        "• <b>подписи Битрикса</b>, которых нет в справочнике — автора "
        "исходящего сообщения бот узнаёт по строке «Имя Фамилия [домен] пишет:»;\n"
        "• <b>чужие боты</b>, писавшие в чаты, — кто за ними стоит, "
        "по ID не видно.\n\n"
        "Пока не разберётесь, такие сообщения не попадают в отчёты по людям, "
        "а сообщения ботов не считаются ни клиентскими, ни нашими.\n\n"
        "<i>Служебные ответы Битрикса — «Вы не авторизованы», «Вы уже "
        "привязаны к этому чату» и прочая переписка с порталом при "
        "подключении чата — в эту очередь не попадают: бот отсеивает их сам.</i>\n\n"
        "<i>Людей здесь нет: их бот относит к клиентам сам, а ошибку видно "
        "в переписке — откройте её и нажмите «👥 Участники».</i>"
    )
    if not groups and not bots:
        text = f"{head}\n\nРазбирать нечего 🎉"
    else:
        # Два счёта не склеиваем: подписи считаются сообщениями, боты — отправителями.
        waiting = []
        if groups:
            waiting.append(f"подписи — <b>{total}</b> сообщ.")
        if bots:
            waiting.append(f"ботов — <b>{len(bots)}</b>")
        text = (
            f"{head}\n\n"
            f"Ждут решения: {'; '.join(waiting)}.\n\n"
            "Выберите строку и укажите, кто это.\n"
            "Решение применится ко всем сообщениям этого отправителя."
        )
    await query.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await query.answer()


async def _render_ignored(query: CallbackQuery) -> None:
    """Подписи, про которые решили «это не сотрудник» — с возвратом обратно."""
    async with session_scope() as session:
        groups = await not_staff_groups(session, limit=PER_PAGE)

    builder = InlineKeyboardBuilder()
    for raw_name, count, anchor in groups:
        label = raw_name or "сообщения без подписи"
        # Нажатие открывает подпись, а не отменяет решение.
        builder.button(
            text=f"{label[:40]} — {count}",
            callback_data=MarkupAction(action="iview", msg_id=anchor).pack(),
        )
    builder.button(text="‹ Назад", callback_data=Nav(to="staff").pack())
    builder.adjust(1)

    await query.message.edit_text(
        "<b>🤖 Не сотрудники</b>\n\n"
        "Подписи, за которыми нет человека: автоматические рассылки "
        "интегратора, боты, посторонние.\n\n"
        "Что это значит:\n\n"
        "• их сообщения не ждут разметки и не считаются потерей качества "
        "в отчётах;\n\n"
        "• они <b>не считаются ответом клиенту</b> — обращение продолжает "
        "ждать человека, и алерт сработает, если никто не ответил;\n\n"
        "• в нагрузку чата такие сообщения входят: они там действительно были;\n\n"
        "• решение действует и на будущие сообщения с той же подписью.\n\n"
        "Нажмите на подпись, чтобы посмотреть её.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await query.answer()


@router.callback_query(StaffAction.filter(F.action == "unresolved"))
async def on_unresolved(query: CallbackQuery, bot_user: BotUser) -> None:
    await _render_unresolved(query)


# Выбор роли и список людей роли — разные действия, а не поле callback:
# новое поле сломало бы уже отправленные кнопки.
ROLE_ACTIONS = {
    "prsp": ROLE_SPECIALIST,
    "prmg": ROLE_MANAGER,
    "prnd": ROLE_UNDECIDED,
}
ROLE_BUTTONS = {
    ROLE_SPECIALIST: "👤 Специалисты",
    ROLE_MANAGER: "👤 Менеджеры",
    ROLE_UNDECIDED: "👤 Роль не определена",
}
# Сколько людей показывать в одной роли: длиннее экран не читается.
ROLE_PAGE = 20


def _role_of(person: Staff) -> str:
    return person.role if person.role in (ROLE_SPECIALIST, ROLE_MANAGER) else ROLE_UNDECIDED


async def _next_anchor(session, anchor_id: int) -> int | None:
    """Якорь следующей группы очереди по кругу. None — группа единственная."""
    anchors = [anchor for _, _, anchor in await unresolved_groups(session, limit=PER_PAGE)]
    if len(anchors) < 2:
        return None
    try:
        index = anchors.index(anchor_id)
    except ValueError:
        return anchors[0]
    return anchors[(index + 1) % len(anchors)]


async def _render_pick(query: CallbackQuery, anchor_id: int, role: str | None) -> None:
    """Карточка группы: сначала роли, по нажатию роли — люди этой роли."""
    async with session_scope() as session:
        anchor = await session.get(Attribution, anchor_id)
        if anchor is None or anchor.staff_id is not None:
            await query.answer("Уже разобрано", show_alert=True)
            await _render_unresolved(AnsweredAlready(query))
            return
        raw_name = anchor.raw_name
        count = await group_size(session, anchor_id)

        # Без текста подпись ничего не говорит: показываем начало сообщения,
        # полная переписка — кнопкой.
        message = await session.get(Message, anchor_id)
        chat = await session.get(Chat, message.chat_id) if message else None
        excerpt = snippet(message, limit=300) if message else ""
        calendar_cfg = await get_section(session, "work_calendar")

        people = (
            await session.scalars(
                select(Staff).where(Staff.active.is_(True)).order_by(Staff.full_name)
            )
        ).all()
        for person in people:
            session.expunge(person)
        skip_to = await _next_anchor(session, anchor_id)

    by_role: dict[str, list[Staff]] = {}
    for person in people:
        by_role.setdefault(_role_of(person), []).append(person)

    builder = InlineKeyboardBuilder()
    if role is None:
        # Только группы, где есть люди.
        for code, title in ROLE_BUTTONS.items():
            if by_role.get(code):
                builder.button(
                    text=f"{title} — {len(by_role[code])}",
                    callback_data=MarkupAction(
                        action=next(key for key, value in ROLE_ACTIONS.items() if value == code),
                        msg_id=anchor_id,
                    ).pack(),
                )
        # Выход для подписей без верного ответа (например, «Система» от интегратора).
        builder.button(
            text="🚫 Это не сотрудник",
            callback_data=MarkupAction(action="ignore", msg_id=anchor_id).pack(),
        )
        if message is not None:
            # Тот же обработчик, что «Показать переписку» в алертах и отчётах.
            builder.button(
                text="💬 Показать переписку",
                callback_data=AlertAction(
                    action="ctxm", chat_id=message.chat_id, msg_id=message.id
                ).pack(),
            )
        if skip_to is not None:
            # Пропуск ничего не решает: следующая группа очереди по кругу.
            builder.button(
                text="Пропустить ›",
                callback_data=MarkupAction(action="skip", msg_id=skip_to).pack(),
            )
        builder.button(
            text="‹ Назад", callback_data=StaffAction(action="unresolved").pack()
        )
    else:
        for person in by_role.get(role, [])[:ROLE_PAGE]:
            builder.button(
                text=person.full_name[:60],
                callback_data=MarkupAction(
                    action="bind", msg_id=anchor_id, staff_id=person.id
                ).pack(),
            )
        builder.button(
            text="‹ К ролям",
            callback_data=MarkupAction(action="pick", msg_id=anchor_id).pack(),
        )
    builder.adjust(1)

    label = esc(raw_name) if raw_name else "без подписи"
    lines = [f"<b>Кто подписан «{label}»?</b>\n"]
    if message is not None:
        when = fmt_when(message.sent_at, calendar_tz(calendar_cfg), datetime.now(timezone.utc))
        lines.append(f"Чат «{esc(chat.title or '?')}», {when}:")
        lines.append(f"<blockquote>{excerpt}</blockquote>")
    if raw_name:
        lines.append(f"Сообщений с такой подписью: {count}\n")
    else:
        lines.append(f"Сообщений с таким текстом: {count}\n")

    if role is not None:
        lines.append(
            f"<b>{ROLE_BUTTONS[role]}</b> — выберите человека, "
            "и все сообщения этой группы привяжутся к нему."
        )
    elif not people:
        lines.append(
            "<i>Справочник пуст — сначала добавьте сотрудников. Если это вообще "
            "не сотрудник, отметьте кнопкой ниже.</i>"
        )
    else:
        lines.append("Выберите роль, а в ней — человека.\n")
        if not raw_name:
            lines.append(
                "<i>«Без подписи» — сообщение из Битрикса, у которого не "
                "разобралась строка «Имя Фамилия [домен] пишет:». Служебные "
                "ответы портала («Вы не авторизованы», «Вы уже привязаны к "
                "этому чату») бот отсеивает сам и сюда не приводит, так что "
                "здесь — живой текст, у которого просто потерялся автор.</i>\n"
            )
        lines.append(
            "<i>Если это не сотрудник — автоматическая рассылка, бот, посторонний — "
            "отметьте кнопкой: группа уйдёт из очереди вместе с будущими "
            "сообщениями, и решение можно будет отменить.</i>"
        )

    await query.message.edit_text(
        "\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await query.answer()


@router.callback_query(MarkupAction.filter(F.action == "pick"))
async def on_markup_pick(
    query: CallbackQuery, callback_data: MarkupAction, bot_user: BotUser
) -> None:
    if not has_perm(bot_user, Perm.ATTRIBUTION_ASSIGN):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    await _render_pick(query, callback_data.msg_id, None)


@router.callback_query(MarkupAction.filter(F.action.in_(set(ROLE_ACTIONS))))
async def on_markup_role(
    query: CallbackQuery, callback_data: MarkupAction, bot_user: BotUser
) -> None:
    if not has_perm(bot_user, Perm.ATTRIBUTION_ASSIGN):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    await _render_pick(query, callback_data.msg_id, ROLE_ACTIONS[callback_data.action])


@router.callback_query(MarkupAction.filter(F.action == "skip"))
async def on_markup_skip(
    query: CallbackQuery, callback_data: MarkupAction, bot_user: BotUser
) -> None:
    """Следующая группа очереди. Данные не меняет — только экран."""
    if not has_perm(bot_user, Perm.ATTRIBUTION_ASSIGN):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    await _render_pick(query, callback_data.msg_id, None)


@router.callback_query(MarkupAction.filter(F.action == "bind"))
async def on_markup_bind(
    query: CallbackQuery, callback_data: MarkupAction, bot_user: BotUser
) -> None:
    if not has_perm(bot_user, Perm.ATTRIBUTION_ASSIGN):
        await query.answer("Недостаточно прав", show_alert=True)
        return

    async with session_scope() as session:
        person = await session.get(Staff, callback_data.staff_id)
        if person is None:
            await query.answer("Не найдено", show_alert=True)
            return
        # Привязка пишется как ручная, с автором и следом в журнале действий
        # (app/services/attribution.py).
        try:
            count = await bind_raw_name(
                session, bot_user, callback_data.msg_id, callback_data.staff_id
            )
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    if not count:
        await query.answer("Уже разобрано", show_alert=True)
    else:
        await query.answer(
            f"Привязано сообщений: {count} → {person.full_name}", show_alert=True
        )
    await _render_unresolved(AnsweredAlready(query))


@router.callback_query(MarkupAction.filter(F.action == "ignore"))
async def on_markup_ignore(
    query: CallbackQuery, callback_data: MarkupAction, bot_user: BotUser
) -> None:
    async with session_scope() as session:
        try:
            count = await mark_not_staff(session, bot_user, callback_data.msg_id)
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    if not count:
        await query.answer("Уже разобрано", show_alert=True)
    else:
        await query.answer(
            f"Отмечено «не сотрудник»: {count}. Вернуть можно в списке "
            "«🤖 Не сотрудники».",
            show_alert=True,
        )
    await _render_unresolved(AnsweredAlready(query))


@router.callback_query(MarkupAction.filter(F.action == "ignored"))
async def on_markup_ignored(query: CallbackQuery, bot_user: BotUser) -> None:
    await _render_ignored(query)


@router.callback_query(MarkupAction.filter(F.action == "iview"))
async def on_markup_ignored_view(
    query: CallbackQuery, callback_data: MarkupAction, bot_user: BotUser
) -> None:
    async with session_scope() as session:
        anchor = await session.get(Attribution, callback_data.msg_id)
        if anchor is None:
            await query.answer("Не найдено", show_alert=True)
            await _render_ignored(AnsweredAlready(query))
            return
        raw_name = anchor.raw_name
        message = await session.get(Message, callback_data.msg_id)
        chat = await session.get(Chat, message.chat_id) if message else None
        excerpt = snippet(message, limit=300) if message else ""
        calendar_cfg = await get_section(session, "work_calendar")
        count = await not_staff_group_size(session, callback_data.msg_id)

    builder = InlineKeyboardBuilder()
    builder.button(
        text="↩︎ Вернуть в очередь разметки",
        callback_data=MarkupAction(action="restore", msg_id=callback_data.msg_id).pack(),
    )
    if message is not None:
        builder.button(
            text="💬 Показать переписку",
            callback_data=AlertAction(
                action="ctxm", chat_id=message.chat_id, msg_id=message.id
            ).pack(),
        )
    builder.button(
        text="‹ Назад", callback_data=MarkupAction(action="ignored").pack()
    )
    builder.adjust(1)

    label = esc(raw_name) if raw_name else "без подписи"
    lines = [f"<b>🤖 {label}</b>\n", "Отмечено как «не сотрудник».\n"]
    if message is not None:
        when = fmt_when(message.sent_at, calendar_tz(calendar_cfg), datetime.now(timezone.utc))
        lines.append(f"Чат «{esc(chat.title or '?')}», {when}:")
        lines.append(f"<blockquote>{excerpt}</blockquote>")
    lines.append(f"Сообщений с этой подписью: {count}\n")
    lines.append(
        "<i>Их не нужно размечать, и ответом клиенту они не считаются.\n"
        "Вернуть подпись в очередь можно кнопкой ниже.</i>"
    )

    await query.message.edit_text(
        "\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await query.answer()


@router.callback_query(MarkupAction.filter(F.action == "restore"))
async def on_markup_restore(
    query: CallbackQuery, callback_data: MarkupAction, bot_user: BotUser
) -> None:
    async with session_scope() as session:
        try:
            count = await restore_to_queue(session, bot_user, callback_data.msg_id)
        except AccessError as exc:
            await query.answer(str(exc), show_alert=True)
            return

    await query.answer(f"Возвращено в очередь: {count}")
    await _render_ignored(AnsweredAlready(query))
