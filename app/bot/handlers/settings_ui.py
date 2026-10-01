"""Раздел «Настройки»: всё, что меняется без разработчика (docs/SCREENS.md, раздел 0)."""

from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.callbacks import Nav, SettingAction
from app.bot.keyboards import settings_section, settings_sections
from app.db.base import session_scope
from app.db.models import BotUser
from app.services.access import Perm, has_perm
from app.services.settings_rules import SettingError, check_section, validate
from app.services.settings_store import (
    DEFAULTS,
    MODE_LABELS,
    cycle_values,
    field_hint,
    field_label,
    get_section,
    group_index_of,
    grouped_section,
    normalize_mode,
    section_description,
    section_extra,
    section_groups,
    section_label,
    set_value,
    visible_fields,
    visible_sections_only,
)
from app.services.transcript import calendar_tz


def _visible_sections(user: BotUser) -> list[str]:
    # ALERT_CONFIGURE есть у владельца и администратора; без него разделов нет.
    if not has_perm(user, Perm.ALERT_CONFIGURE):
        return []
    # Служебные поля (hidden_fields) на экран не выводятся; раздел без видимых
    # полей не показывается.
    return visible_sections_only(list(DEFAULTS))


router = Router(name="settings")
# Только приватные диалоги: в группы бот не пишет и меню там не показывает.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class SettingForm(StatesGroup):
    waiting_value = State()


async def _after_change(session, section: str, key: str, value) -> None:
    """Побочные действия после записи настройки.

    Включение рассылки или смена её дня помечает текущий выпуск отправленным:
    иначе досылка сочла бы его долгом и отправила отчёт немедленно.
    """
    if section != "digest":
        return

    # Сводки по алертам — то же правило: прошедшие сегодня слоты гасятся,
    # расписание действует со следующего.
    if key == "alerts_digest_times" or (key == "alerts_digest_enabled" and bool(value)):
        from app.services.alert_digest import suppress_passed_slots

        await suppress_passed_slots(session)

    weekly = key == "weekly_day" or (key == "weekly_enabled" and bool(value))
    monthly = key == "monthly_day" or (key == "monthly_enabled" and bool(value))
    if weekly or monthly:
        from app.services.digest import suppress_pending_issue

        await suppress_pending_issue(session, weekly=weekly, monthly=monthly)


def _render_value(value) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "пусто"
    if value is None:
        return "не задано"
    return str(value)


def _hidden(section: str, key: str | None, values: dict) -> bool:
    """Поле убрано с экрана — менять его нельзя ничем, в том числе старой
    кнопкой или подделанным callback."""
    return key is None or key not in visible_fields(section, values)


_HIDDEN_ANSWER = "Это поле больше не настраивается здесь"


def _field_line(section: str, key: str, value) -> str:
    shown = (
        MODE_LABELS.get(normalize_mode(value), str(value))
        if cycle_values(section, key)
        else _render_value(value)
    )
    return f"• {field_label(section, key)}: <b>{shown}</b>"


def _field_back_cb(section: str, key: str | None) -> str:
    """Куда ведёт «Назад» с экрана поля: в разделе-хабе — в блок, где поле живёт."""
    if key is not None and grouped_section(section):
        index = group_index_of(section, key)
        if index is not None:
            return SettingAction(
                action="group", section=section, key=str(index)
            ).pack()
    return SettingAction(action="section", section=section).pack()


@router.callback_query(Nav.filter(F.to == "settings"))
async def on_settings(query: CallbackQuery, bot_user: BotUser) -> None:
    sections = [(key, section_label(key)) for key in _visible_sections(bot_user)]
    text = (
        "<b>Настройки</b>\n\n"
        "Всё, что здесь есть, меняется кнопками — без разработчика.\n\n"
        "Секретов тут нет: токены и ключи живут в конфигурации сервера "
        "и сюда не попадают."
    )
    await query.message.edit_text(
        text, reply_markup=settings_sections(sections), parse_mode="HTML"
    )
    await query.answer()


@router.callback_query(SettingAction.filter(F.action == "section"))
async def on_section(
    query: CallbackQuery,
    callback_data: SettingAction,
    bot_user: BotUser,
    state: FSMContext | None = None,
) -> None:
    if callback_data.section not in _visible_sections(bot_user):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    # «Назад» из формы ввода ведёт сюда — ожидание текста надо снять,
    # иначе следующее сообщение пользователя утечёт в брошенную форму.
    if state is not None:
        await state.clear()
    async with session_scope() as session:
        values = await get_section(session, callback_data.section)

    # Служебные поля (hidden_fields) на экран не выводятся; раздел без видимых
    # полей не показывается.
    shown_keys = visible_fields(callback_data.section, values)

    lines = [f"<b>{section_label(callback_data.section)}</b>"]
    description = section_description(callback_data.section)
    if description:
        lines.append(f"<i>{description}</i>")

    # Раздел с именованными блоками — хаб: сперва кнопки блоков со статусом,
    # поля — внутри блока.
    if grouped_section(callback_data.section):
        builder = InlineKeyboardBuilder()
        lines.append("")
        for index, (title, keys) in enumerate(
            section_groups(callback_data.section, shown_keys)
        ):
            # Статус блока — только по его тумблеру «…_enabled»/«enabled»,
            # а не по первому булеву полю.
            status = ""
            for key in keys:
                if key.endswith("enabled") and isinstance(values.get(key), bool):
                    status = " — вкл" if values[key] else " — выкл"
                    break
            lines.append(f"• {title}{status}")
            # На кнопке — короткое имя блока, пояснение после тире
            # остаётся в тексте выше.
            builder.button(
                text=(title or "Прочее").split(" — ")[0],
                callback_data=SettingAction(
                    action="group", section=callback_data.section, key=str(index)
                ).pack(),
            )
        lines.append("")
        lines.append("<i>Выберите блок — его настройки внутри.</i>")
        builder.button(text="‹ Назад", callback_data=Nav(to="settings").pack())
        builder.adjust(1)
        await query.message.edit_text(
            "\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML"
        )
        await query.answer()
        return

    # Поля — блоками с заголовками; кнопки в том же порядке, что и блоки.
    ordered_keys: list[str] = []
    for title, keys in section_groups(callback_data.section, shown_keys):
        lines.append("")
        if title:
            lines.append(f"<b>{title}</b>")
        for key in keys:
            lines.append(_field_line(callback_data.section, key, values[key]))
            ordered_keys.append(key)

    if not shown_keys:
        lines.append("")
        lines.append("<i>В этом разделе пока нечего настраивать.</i>")

    # Не-настройка, но часть картины: история графика под полями календаря.
    extra = section_extra(callback_data.section, values)
    if extra:
        lines.append("")
        lines.append(extra)

    await query.message.edit_text(
        "\n".join(lines),
        reply_markup=settings_section(callback_data.section, ordered_keys, values),
        parse_mode="HTML",
    )
    await query.answer()


@router.callback_query(SettingAction.filter(F.action == "group"))
async def on_group(
    query: CallbackQuery,
    callback_data: SettingAction,
    bot_user: BotUser,
    state: FSMContext | None = None,
) -> None:
    if callback_data.section not in _visible_sections(bot_user):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    if state is not None:
        await state.clear()
    async with session_scope() as session:
        values = await get_section(session, callback_data.section)

    shown_keys = visible_fields(callback_data.section, values)
    groups = section_groups(callback_data.section, shown_keys)
    try:
        index = int(callback_data.key or "")
    except ValueError:
        index = -1
    if not 0 <= index < len(groups):
        await query.answer("Этого блока больше нет", show_alert=True)
        return

    title, keys = groups[index]
    lines = [f"<b>{section_label(callback_data.section)}</b>"]
    if title:
        lines.append(f"<b>{title}</b>")
    lines.append("")
    for key in keys:
        lines.append(_field_line(callback_data.section, key, values[key]))

    await query.message.edit_text(
        "\n".join(lines),
        reply_markup=settings_section(
            callback_data.section,
            list(keys),
            values,
            back_cb=SettingAction(
                action="section", section=callback_data.section
            ).pack(),
        ),
        parse_mode="HTML",
    )
    await query.answer()


# Ключ-заглушка формы: правится не поле, а момент начала версии раздела.
_SINCE_KEY = "__since__"


@router.callback_query(SettingAction.filter(F.action == "since"))
async def on_since(
    query: CallbackQuery, callback_data: SettingAction, state: FSMContext, bot_user: BotUser
) -> None:
    """«Действует с …»: момент начала нынешней версии настроек раздела."""
    from app.services.settings_store import previous_version_since, versioned_fields

    if callback_data.section not in _visible_sections(bot_user):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    if not versioned_fields(callback_data.section):
        await query.answer("У этого раздела нет версий", show_alert=True)
        return

    async with session_scope() as session:
        values = await get_section(session, callback_data.section)
    tz = calendar_tz(values if callback_data.section == "work_calendar" else {})

    from app.services.versioning import parse_since

    since = parse_since(values.get("since"), values.get("timezone"))
    previous = previous_version_since(callback_data.section, values)

    await state.set_state(SettingForm.waiting_value)
    await state.update_data(section=callback_data.section, key=_SINCE_KEY)

    lines = [
        f"<b>{section_label(callback_data.section)} — действует с</b>",
        "",
        "Сейчас: "
        + (
            f"<b>{since.astimezone(tz):%d.%m.%Y %H:%M}</b>"
            if since is not None
            else "<b>всегда</b> (настройку ещё не меняли)"
        ),
    ]
    if previous is not None:
        lines.append(
            f"Предыдущая версия действовала с {previous.astimezone(tz):%d.%m.%Y %H:%M} "
            "— раньше этого момента сдвинуть нельзя."
        )
    lines += [
        "",
        "Отправьте момент сообщением: <code>ДД.ММ ЧЧ:ММ</code> "
        "(например <code>03.09 10:00</code>), либо слова "
        "<code>сейчас</code> или <code>с начала дня</code>.",
        "",
        "<i>Обращения до этого момента считаются по предыдущей версии, "
        "после — по нынешней. Момент в будущем задать нельзя.</i>",
    ]

    builder = InlineKeyboardBuilder()
    builder.button(
        text="‹ Назад",
        callback_data=SettingAction(
            action="section", section=callback_data.section
        ).pack(),
    )
    await query.message.edit_text(
        "\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await query.answer()


# Кнопочные значения полей: подпись и то, что уйдёт в валидацию.
_BOOL_CHOICES = (("yes", "Да", True), ("no", "Нет", False))


def _render_choice_screen(section: str, key: str, current, cycle) -> tuple[str, object]:
    """Экран поля с выбором значения кнопками (да/нет или режимы)."""
    builder = InlineKeyboardBuilder()
    if cycle:
        shown = MODE_LABELS.get(normalize_mode(current), str(current))
        for value in cycle:
            mark = "• " if normalize_mode(current) == value else ""
            builder.button(
                text=f"{mark}{MODE_LABELS.get(value, value).capitalize()}",
                callback_data=SettingAction(
                    action="setv", section=section, key=key, value=value
                ).pack(),
            )
    else:
        shown = "да" if current else "нет"
        for value, label, matches in _BOOL_CHOICES:
            mark = "• " if bool(current) is matches else ""
            builder.button(
                text=f"{mark}{label}",
                callback_data=SettingAction(
                    action="setv", section=section, key=key, value=value
                ).pack(),
            )
    builder.button(text="‹ Назад", callback_data=_field_back_cb(section, key))
    builder.adjust(2, 1)

    text = (
        f"<b>{field_label(section, key)}</b>\n"
        f"Текущее значение: <b>{shown}</b>\n\n"
        "Выберите новое значение кнопкой."
    )
    return text, builder.as_markup()


@router.callback_query(SettingAction.filter(F.action == "setv"))
async def on_set_value(
    query: CallbackQuery, callback_data: SettingAction, bot_user: BotUser
) -> None:
    if callback_data.section not in _visible_sections(bot_user):
        await query.answer("Недостаточно прав", show_alert=True)
        return

    raw = callback_data.value or ""
    mapped = {"yes": True, "no": False}.get(raw, raw)
    async with session_scope() as session:
        if _hidden(
            callback_data.section,
            callback_data.key,
            await get_section(session, callback_data.section),
        ):
            await query.answer(_HIDDEN_ANSWER, show_alert=True)
            return
        try:
            # Та же предметная валидация, что у текста: «алерты в группу»
            # без заданной группы обязаны объясниться, а не молча включиться.
            parsed = validate(callback_data.section, callback_data.key, mapped)
        except SettingError as exc:
            await query.answer(str(exc), show_alert=True)
            return

        # Нажатие уже выбранного значения — не ошибка: Telegram отверг бы
        # правку тем же текстом.
        current = (await get_section(session, callback_data.section)).get(
            callback_data.key
        )
        if current == parsed:
            await query.answer("Уже так — ничего не изменилось")
            return

        await set_value(
            session, callback_data.section, callback_data.key, parsed, actor_id=bot_user.id
        )
        await _after_change(session, callback_data.section, callback_data.key, parsed)
        values = await get_section(session, callback_data.section)

    cycle = cycle_values(callback_data.section, callback_data.key)
    text, markup = _render_choice_screen(
        callback_data.section, callback_data.key, values.get(callback_data.key), cycle
    )
    try:
        await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass  # экран уже в этом виде — сохранённое значение важнее перерисовки
    await query.answer("Сохранено")


@router.callback_query(SettingAction.filter(F.action == "edit"))
async def on_edit(
    query: CallbackQuery, callback_data: SettingAction, state: FSMContext, bot_user: BotUser
) -> None:
    if callback_data.section not in _visible_sections(bot_user):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    async with session_scope() as session:
        values = await get_section(session, callback_data.section)

    if _hidden(callback_data.section, callback_data.key, values):
        await query.answer(_HIDDEN_ANSWER, show_alert=True)
        return
    current = values.get(callback_data.key)

    # Поля с конечным набором значений (да/нет, режимы) правятся кнопками
    # на своём экране — форма ввода текста остаётся числам, времени и спискам.
    cycle = cycle_values(callback_data.section, callback_data.key)
    if cycle or isinstance(current, bool):
        text, markup = _render_choice_screen(
            callback_data.section, callback_data.key, current, cycle
        )
        await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await query.answer()
        return

    await state.set_state(SettingForm.waiting_value)
    await state.update_data(section=callback_data.section, key=callback_data.key)

    # «Назад» — в раздел или блок хаба, откуда пришли, а не в корень настроек.
    builder = InlineKeyboardBuilder()
    builder.button(
        text="‹ Назад",
        callback_data=_field_back_cb(callback_data.section, callback_data.key),
    )
    await query.message.edit_text(
        f"<b>{field_label(callback_data.section, callback_data.key)}</b>\n\n"
        f"Текущее значение: {_render_value(current)}\n\n"
        f"Отправьте новое значение сообщением.\n"
        f"{field_hint(callback_data.section, callback_data.key)}",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await query.answer()


async def _apply_since(
    message: Message, state: FSMContext, bot_user: BotUser, section: str
) -> None:
    """Записать новый момент начала версии раздела, проверив порядок."""
    from app.services.settings_rules import as_version_moment
    from app.services.settings_store import previous_version_since, set_version_since

    async with session_scope() as session:
        values = await get_section(session, section)
        tz = calendar_tz(values if section == "work_calendar" else {})
        try:
            moment = as_version_moment(
                message.text or "", now=datetime.now(tz).date(), tzinfo=tz
            )
            previous = previous_version_since(section, values)
            if previous is not None and moment <= previous:
                raise SettingError(
                    "Раньше предыдущей версии: она действует с "
                    f"{previous.astimezone(tz):%d.%m.%Y %H:%M}. Версии идут "
                    "по порядку, иначе непонятно, что действовало между ними"
                )
        except SettingError as exc:
            # Текст ошибки содержит <code> — нужен parse_mode; кнопка — чтобы
            # не мотать диалог вверх к форме.
            builder = InlineKeyboardBuilder()
            builder.button(text="‹ К разделу", callback_data=_field_back_cb(section, None))
            await message.answer(
                f"❌ {exc}\n\nМомент не изменён, попробуйте ещё раз.",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
            return
        await set_version_since(session, section, moment, actor_id=bot_user.id)

    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(
        text="‹ К разделу",
        callback_data=SettingAction(action="section", section=section).pack(),
    )
    await message.answer(
        f"✅ Нынешние настройки раздела «{section_label(section)}» действуют "
        f"с {moment.astimezone(tz):%d.%m.%Y %H:%M}.\n\n"
        "<i>Обращения до этого момента пересчитаются по предыдущей версии "
        "в ближайшую минуту.</i>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@router.message(SettingForm.waiting_value)
async def on_value(message: Message, state: FSMContext, bot_user: BotUser) -> None:
    data = await state.get_data()
    section, key = data.get("section", ""), data.get("key", "")

    if key == _SINCE_KEY:
        await _apply_since(message, state, bot_user, section)
        return

    async with session_scope() as session:
        values = await get_section(session, section)
        if _hidden(section, key, values):
            await state.clear()
            await message.answer(f"❌ {_HIDDEN_ANSWER}.")
            return
        try:
            # Проверка предметная, а не только по типу: несуществующий пояс
            # или перевёрнутый рабочий день ломают систему тихо и надолго.
            parsed = validate(section, key, message.text or "")
            check_section(section, {**values, key: parsed})
        except SettingError as exc:
            builder = InlineKeyboardBuilder()
            builder.button(text="‹ К разделу", callback_data=_field_back_cb(section, key))
            await message.answer(
                f"❌ {exc}\n\nЗначение не изменено, попробуйте ещё раз.",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
            return
        await set_value(session, section, key, parsed, actor_id=bot_user.id)
        await _after_change(session, section, key, parsed)

    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text="‹ К разделу", callback_data=_field_back_cb(section, key))
    await message.answer(
        f"✅ {field_label(section, key)} → {_render_value(parsed)}",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
