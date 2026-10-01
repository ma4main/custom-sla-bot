"""Сборка клавиатур. Меню собирается под роль: разделов, которых пользователю
нельзя, он не видит (docs/SCREENS.md, раздел 0)."""

from __future__ import annotations

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.callbacks import (
    ChatAction,
    DismissedAction,
    LabAction,
    MarkupAction,
    Nav,
    ReportAction,
    SettingAction,
    StaffAction,
    UserAction,
)
from app.db.models import BotRole, BotUser, BotUserState, Chat, ChatState, Staff
from app.services.access import (
    ASSIGNABLE_ORDER,
    Perm,
    effective_permissions,
    role_label,
)
from app.services.staff_roles import (
    ROLE_MANAGER,
    ROLE_SPECIALIST,
    ROLE_UNDECIDED,
    SOURCE_MANUAL,
)
from app.services.chats import STATE_LABELS
from app.services.settings_store import field_label

# Раздел меню → (подпись, право, которое его открывает)
_SECTIONS: list[tuple[str, str, str | None]] = [
    ("reports", "📊 Отчёты", Perm.REPORT_SELF),
    ("chats", "💬 Чаты", Perm.CHAT_MANAGE),
    ("staff", "👥 Сотрудники", Perm.STAFF_MANAGE),
    ("users", "🛡 Пользователи бота", Perm.USER_MANAGE),
    ("settings", "⚙️ Настройки", Perm.SYSTEM_SETTINGS),
    ("health", "🩺 Состояние системы", Perm.SYSTEM_HEALTH),
    # Справка — всем активным учётным записям, без отдельного права.
    ("help", "📖 Справка", None),
]


def main_menu(user: BotUser) -> InlineKeyboardMarkup:
    perms = effective_permissions(user)
    builder = InlineKeyboardBuilder()
    if user.state is not BotUserState.ACTIVE:
        return builder.as_markup()
    for key, label, required in _SECTIONS:
        if required is None or required in perms:
            builder.button(text=label, callback_data=Nav(to=key).pack())
    builder.adjust(1)
    return builder.as_markup()


def back_to(target: str = "main", label: str = "‹ Назад") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data=Nav(to=target).pack())
    return builder.as_markup()


def _pagination(
    builder: InlineKeyboardBuilder,
    factory,
    *,
    page: int,
    total: int,
    per_page: int,
    **kwargs,
) -> None:
    pages = max(1, -(-total // per_page))
    if pages <= 1:
        return
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(
            InlineKeyboardButton(text="‹", callback_data=factory(page=page - 1, **kwargs).pack())
        )
    row.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        row.append(
            InlineKeyboardButton(text="›", callback_data=factory(page=page + 1, **kwargs).pack())
        )
    builder.row(*row)


# ── Чаты ──────────────────────────────────────────────────────


def chats_overview(counts: dict[ChatState, int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    discovered = counts.get(ChatState.DISCOVERED, 0)
    if discovered:
        builder.button(
            text=f"✅ Включить все обнаруженные ({discovered})",
            callback_data=ChatAction(action="track_all").pack(),
        )
    for state in (ChatState.DISCOVERED, ChatState.TRACKED, ChatState.PAUSED, ChatState.ARCHIVED):
        # «Обнаружен» при включённом автовключении почти всегда пуст —
        # группу показываем, только если в ней кто-то есть.
        if state is ChatState.DISCOVERED and not discovered:
            continue
        builder.button(
            text=f"{STATE_LABELS[state]} — {counts.get(state, 0)}",
            callback_data=ChatAction(action="list", value=state.value, page=0).pack(),
        )
    builder.button(text="🔍 Найти чат", callback_data=ChatAction(action="search").pack())
    builder.button(text="‹ Назад", callback_data=Nav(to="main").pack())
    builder.adjust(1)
    return builder.as_markup()


# Состояние чата — одним знаком в строке результата поиска: список идёт
# по всем состояниям сразу, и без знака не понять, почему чата нет в отчётах.
_STATE_MARKS = {
    ChatState.TRACKED: "✅",
    ChatState.PAUSED: "⏸",
    ChatState.ARCHIVED: "📦",
    ChatState.DISCOVERED: "🆕",
}


def chats_search_results(chats: list[Chat]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for chat in chats:
        title = chat.title or f"чат {chat.tg_chat_id}"
        builder.button(
            text=f"{_STATE_MARKS.get(chat.state, '')} {title[:56]}",
            callback_data=ChatAction(action="view", chat_id=chat.id, page=0).pack(),
        )
    builder.button(text="🔍 Искать ещё", callback_data=ChatAction(action="search").pack())
    builder.button(text="‹ К чатам", callback_data=Nav(to="chats").pack())
    builder.adjust(1)
    return builder.as_markup()


def chats_list(
    chats: list[Chat], state: ChatState, page: int, total: int, per_page: int = 8
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for chat in chats:
        title = chat.title or f"чат {chat.tg_chat_id}"
        builder.button(
            text=title[:60],
            callback_data=ChatAction(action="view", chat_id=chat.id, page=page).pack(),
        )
    builder.adjust(1)
    _pagination(
        builder,
        lambda page, **kw: ChatAction(action="list", value=state.value, page=page),
        page=page,
        total=total,
        per_page=per_page,
    )
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data=Nav(to="chats").pack()))
    return builder.as_markup()


def chat_card(chat: Chat, page: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    # Из любого состояния есть путь в любое другое.
    if chat.state in (ChatState.DISCOVERED, ChatState.PAUSED, ChatState.ARCHIVED):
        builder.button(
            text="✅ Включить в анализ",
            callback_data=ChatAction(action="track", chat_id=chat.id, page=page).pack(),
        )
    if chat.state is ChatState.TRACKED:
        builder.button(
            text="⏸ Поставить на паузу",
            callback_data=ChatAction(action="pause", chat_id=chat.id, page=page).pack(),
        )
    if chat.state in (ChatState.DISCOVERED, ChatState.TRACKED, ChatState.PAUSED):
        builder.button(
            text="📦 Убрать в архив",
            callback_data=ChatAction(action="archive", chat_id=chat.id, page=page).pack(),
        )
    if chat.state is ChatState.ARCHIVED:
        # Удалить можно только архивный чат: удаление чата в анализе унесло бы живые данные.
        builder.button(
            text="🗑 Удалить чат",
            callback_data=ChatAction(action="delete", chat_id=chat.id, page=page).pack(),
        )
    builder.button(
        text="‹ К списку",
        callback_data=ChatAction(action="list", value=chat.state.value, page=page).pack(),
    )
    builder.adjust(1)
    return builder.as_markup()


# ── Сотрудники ────────────────────────────────────────────────


# Срез списка по роли → action пагинации. Роль в отдельное поле callback
# не выносится сознательно: новое поле в схеме сломало бы старые кнопки.
STAFF_LIST_ACTIONS = {
    None: "list",
    ROLE_SPECIALIST: "list_s",
    ROLE_MANAGER: "list_m",
    ROLE_UNDECIDED: "list_u",
}


def staff_root(
    *, specialists: int, managers: int, undecided: int, unresolved: int, not_staff: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"🎓 Специалисты — {specialists}",
        callback_data=StaffAction(action="list_s").pack(),
    )
    builder.button(
        text=f"🤝 Менеджеры — {managers}",
        callback_data=StaffAction(action="list_m").pack(),
    )
    builder.button(
        text=f"❓ Роль пока не определена — {undecided}",
        callback_data=StaffAction(action="list_u").pack(),
    )
    builder.button(
        text="➕ Добавить сотрудника", callback_data=StaffAction(action="add").pack()
    )
    builder.button(
        text=f"🔍 Не определили, кто это — {unresolved}",
        callback_data=StaffAction(action="unresolved").pack(),
    )
    if not_staff:
        # Подписи без человека — отдельная категория: их сообщения в чатах есть,
        # а правила для них другие.
        builder.button(
            text=f"🤖 Не сотрудники — {not_staff}",
            callback_data=MarkupAction(action="ignored").pack(),
        )
    builder.button(text="‹ Назад", callback_data=Nav(to="main").pack())
    builder.adjust(1)
    return builder.as_markup()


def staff_group(
    staff: list[Staff], page: int, total: int, role_filter: str | None
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    list_action = STAFF_LIST_ACTIONS.get(role_filter, "list")
    for person in staff:
        mark = "" if person.active else "⛔ "
        builder.button(
            text=f"{mark}{person.full_name}"[:60],
            callback_data=StaffAction(action="view", staff_id=person.id, page=page).pack(),
        )
    builder.adjust(1)
    _pagination(
        builder,
        lambda page, **kw: StaffAction(action=list_action, page=page),
        page=page,
        total=total,
        per_page=8,
    )
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data=Nav(to="staff").pack()))
    return builder.as_markup()


def staff_card(person: Staff, page: int, role_button: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=role_button,
        callback_data=StaffAction(action="role", staff_id=person.id, page=page).pack(),
    )
    builder.button(
        text="✍️ Добавить написание имени",
        callback_data=StaffAction(action="alias", staff_id=person.id, page=page).pack(),
    )
    if person.active:
        builder.button(
            text="🚪 Больше не работает",
            callback_data=StaffAction(action="deactivate", staff_id=person.id, page=page).pack(),
        )
    else:
        builder.button(
            text="↩︎ Вернуть в работу",
            callback_data=StaffAction(action="activate", staff_id=person.id, page=page).pack(),
        )
    # Назад — в ту группу, где человек сейчас состоит: после смены роли это
    # уже другая группа, и попасть надо именно в неё.
    builder.button(
        text="‹ К списку",
        callback_data=StaffAction(
            action=STAFF_LIST_ACTIONS.get(person.role or ROLE_UNDECIDED, "list"), page=page
        ).pack(),
    )
    builder.adjust(1)
    return builder.as_markup()


def staff_role_choice(person: Staff, page: int) -> InlineKeyboardMarkup:
    manual = person.role_source == SOURCE_MANUAL
    options = (
        ("role_s", "🎓 Специалист", manual and person.role == ROLE_SPECIALIST),
        ("role_m", "🤝 Менеджер", manual and person.role == ROLE_MANAGER),
        ("role_a", "🤖 Определять автоматически", not manual),
    )
    builder = InlineKeyboardBuilder()
    for action, label, chosen in options:
        builder.button(
            text=f"{'• ' if chosen else ''}{label}",
            callback_data=StaffAction(action=action, staff_id=person.id, page=page).pack(),
        )
    builder.button(
        text="‹ Назад",
        callback_data=StaffAction(action="view", staff_id=person.id, page=page).pack(),
    )
    builder.adjust(1)
    return builder.as_markup()


# ── Пользователи бота ─────────────────────────────────────────


def users_list(
    users: list[BotUser], page: int, total: int, pending: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user in users:
        name = user.display_name or user.username or str(user.tg_user_id)
        # Отключённый остаётся в списке (роль и привязка нужны в истории), но помечен.
        mark = "" if user.state is BotUserState.ACTIVE else "⛔ "
        builder.button(
            text=f"{mark}{name} — {role_label(user.role)}"[:60],
            callback_data=UserAction(action="view", user_id=user.id, page=page).pack(),
        )
    builder.adjust(1)
    _pagination(
        builder,
        lambda page, **kw: UserAction(action="list", page=page),
        page=page,
        total=total,
        per_page=8,
    )
    builder.row(
        InlineKeyboardButton(
            text=f"📥 Заявки на доступ — {pending}",
            callback_data=UserAction(action="pending").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🎟 Создать приглашение", callback_data=UserAction(action="invite_menu").pack()
        )
    )
    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data=Nav(to="main").pack()))
    return builder.as_markup()


def user_card(
    user: BotUser,
    page: int,
    is_self: bool,
    *,
    can_transfer: bool = True,
    can_manage: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if is_self and can_manage:
        # Себя привязывает только тот, у кого USER_MANAGE: сотрудник, связав себя
        # с чужой записью, увидел бы чужие показатели.
        builder.button(
            text="🔗 Привязать к сотруднику",
            callback_data=UserAction(action="link", user_id=user.id, page=page).pack(),
        )
    if not is_self:
        for role in ASSIGNABLE_ORDER:
            if user.role is not role:
                builder.button(
                    text=f"Сделать: {role_label(role)}",
                    callback_data=UserAction(
                        action="set_role", user_id=user.id, page=page, role=role.value
                    ).pack(),
                )
        # Кнопки владения — только владельцу; авторизация всё равно в обработчике.
        if can_transfer:
            if user.role is not BotRole.OWNER:
                builder.button(
                    text="👑 Сделать совладельцем",
                    callback_data=UserAction(
                        action="co_owner", user_id=user.id, page=page
                    ).pack(),
                )
            builder.button(
                text="👑 Передать владение",
                callback_data=UserAction(action="transfer", user_id=user.id, page=page).pack(),
            )
        # Без этой связи персональные отчёты не работают.
        builder.button(
            text="🔗 Привязать к сотруднику",
            callback_data=UserAction(action="link", user_id=user.id, page=page).pack(),
        )
        if user.state is BotUserState.DISABLED:
            builder.button(
                text="✅ Включить",
                callback_data=UserAction(action="enable", user_id=user.id, page=page).pack(),
            )
        else:
            builder.button(
                text="⛔ Отключить",
                callback_data=UserAction(action="disable", user_id=user.id, page=page).pack(),
            )
    # Доступно и себе, и управляющему; предохранитель — в обработчике.
    builder.button(
        text=f"🔔 Уведомления в личные сообщения: {'да' if user.notify_personal else 'нет'}",
        callback_data=UserAction(action="notify", user_id=user.id, page=page).pack(),
    )
    builder.button(text="‹ К списку", callback_data=UserAction(action="list", page=page).pack())
    builder.adjust(1)
    return builder.as_markup()


def invite_roles() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    # Роль владельца через приглашение не выдаётся — только передаётся.
    for role in ASSIGNABLE_ORDER:
        builder.button(
            text=f"🎟 {role_label(role)}",
            callback_data=UserAction(action="invite_create", role=role.value).pack(),
        )
    builder.button(text="‹ Назад", callback_data=UserAction(action="list").pack())
    builder.adjust(1)
    return builder.as_markup()


def invite_created(link: str) -> InlineKeyboardMarkup:
    """«Скопировать» — CopyTextButton, а не ссылка: приглашение одноразовое,
    и переход по нему погасил бы его на том, кто его выпустил."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Скопировать ссылку", copy_text=CopyTextButton(text=link))
    builder.button(
        text="🎟 Выпустить ещё", callback_data=UserAction(action="invite_menu").pack()
    )
    builder.button(text="‹ Назад", callback_data=UserAction(action="list").pack())
    builder.adjust(1)
    return builder.as_markup()


def confirm(action_cb: str, cancel_cb: str, label: str = "Да, подтверждаю") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data=action_cb)
    builder.button(text="Отмена", callback_data=cancel_cb)
    builder.adjust(1)
    return builder.as_markup()


# ── Настройки ─────────────────────────────────────────────────


def settings_sections(sections: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, label in sections:
        builder.button(text=label, callback_data=SettingAction(action="section", section=key).pack())
    builder.button(text="‹ Назад", callback_data=Nav(to="main").pack())
    builder.adjust(1)
    return builder.as_markup()


def settings_section(
    section: str,
    fields: list[str],
    values: dict | None = None,
    back_cb: str | None = None,
) -> InlineKeyboardMarkup:
    # Каждое поле — карандаш: значение выбирается на экране поля, а не
    # переключателем в списке. back_cb ведёт в хаб раздела, а не в корень настроек.
    builder = InlineKeyboardBuilder()
    for field in fields:
        builder.button(
            text=f"✏️ {field_label(section, field)}",
            callback_data=SettingAction(action="edit", section=section, key=field).pack(),
        )
    # Разделы, участвующие в расчёте сроков, хранят версии с датой начала действия.
    from app.services.settings_store import versioned_fields

    if versioned_fields(section):
        builder.button(
            text="🕐 Действует с …",
            callback_data=SettingAction(action="since", section=section).pack(),
        )
    builder.button(text="‹ Назад", callback_data=back_cb or Nav(to="settings").pack())
    builder.adjust(1)
    return builder.as_markup()


# ── Отчёты ────────────────────────────────────────────────────


def reports_menu(user: BotUser) -> InlineKeyboardMarkup:
    perms = effective_permissions(user)
    builder = InlineKeyboardBuilder()
    if Perm.REPORT_ALL_CHATS in perms:
        builder.button(
            text="📈 Сводный по всем чатам",
            callback_data=ReportAction(action="scope", scope="all").pack(),
        )
    if Perm.REPORT_CHAT in perms:
        builder.button(
            text="💬 По чату", callback_data=ReportAction(action="scope", scope="chat").pack()
        )
    if Perm.REPORT_ANY_STAFF in perms:
        builder.button(
            text="👤 По сотруднику",
            callback_data=ReportAction(action="scope", scope="staff").pack(),
        )
    if Perm.REPORT_SELF in perms and Perm.REPORT_ANY_STAFF not in perms:
        # «Мои показатели» — только без права «По сотруднику». Обработчик открыт
        # всем: старые сообщения с кнопкой продолжают работать.
        builder.button(
            text="🙋 Мои показатели",
            callback_data=ReportAction(action="scope", scope="self").pack(),
        )
    if Perm.REPORT_ALL_CHATS in perms:
        builder.button(
            text="🔥 Требует внимания (сейчас)",
            callback_data=LabAction(kind="attention").pack(),
        )
        # «Скорость по сотрудникам» и «Работа вне графика» — внутри «По сотруднику».
        builder.button(
            text="🕐 Когда пишут клиенты",
            callback_data=LabAction(kind="load").pack(),
        )
        builder.button(
            text="🧾 Сводка по алертам",
            callback_data=LabAction(kind="adig").pack(),
        )
        builder.button(
            text="✋ Снятые нарушения",
            callback_data=DismissedAction().pack(),
        )
    builder.button(text="‹ Назад", callback_data=Nav(to="main").pack())
    builder.adjust(1)
    return builder.as_markup()

