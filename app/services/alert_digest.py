"""Сводки по алертам: что ещё ждёт ответа и чем закончились сработавшие алерты.

Важно: горящее показывается в каждой сводке, пока не закроется; закрытие — ровно
один раз, в ближайшей сводке после него (окно «с прошлого выпуска», `last_at`).
Статус выводится из текущего состояния обращения, в alert_log не пишется.
Доставка без повторов: недоставленная сводка теряет смысл к следующей.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as _time, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import structlog
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import or_, select

from app.bot.callbacks import AlertAction, LabAction
from app.db.models import (
    AlertLog,
    BotUser,
    BreachDismissal,
    Chat,
    Interaction,
    InteractionState,
    Setting,
    Staff,
)
from app.services.alerts import KIND_NO_REACTION, KIND_NO_SUBSTANTIVE
from app.services.calendar import (
    CalendarHistory,
    business_seconds,
    is_workday,
    response_deadline,
)
from app.services.episodes import handoff_deadline
from app.services.settings_store import get_section
from app.services.transcript import calendar_tz, escape, fmt_duration

log = structlog.get_logger(__name__)

# Память сводки (какую дату каждый слот уже отработал) — не настройка человека,
# поэтому не в DEFAULTS.
STATE_KEY = "alert_digest_runtime"

# Момент предыдущего выпуска в том же словаре; не пересекается с ключами-временами слотов.
LAST_AT_KEY = "last_at"

# Времена по умолчанию; настраиваются в `digest.alerts_digest_times`.
# Должно совпадать с DEFAULTS в settings_store.
DEFAULT_TIMES: tuple[str, ...] = ("10:10", "14:30", "18:30")
MAX_SLOTS = 5

# Слот — само время «ЧЧ:ММ». Именованные слоты в сохранённом состоянии
# переводятся во время, иначе отработавший слот ушёл бы повторно.
_LEGACY_SLOTS = {"morning": "10:35", "midday": "14:30", "evening": "18:30"}

MANUAL_SLOT = "manual"

# Глубина просмотра журнала алертов сверх настроенного окна ожидания.
LOOKBACK_DAYS = 30

# Telegram режет сообщение на 4096 символах.
MAX_PER_BLOCK = 10
MAX_BUTTONS = 12

_KIND_LABELS = {
    KIND_NO_REACTION: "нет реакции",
    KIND_NO_SUBSTANTIVE: "нет ответа специалиста",
}

# Один callback у кнопки «🧾 Сводка по алертам» и у «Обновить» под сводкой.
MANUAL_KIND = "adig"


@dataclass
class DigestView:
    """Готовая сводка: текст и кнопки выписок под ним (подпись, callback)."""

    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)


def digest_keyboard(view: DigestView, back_cb: str | None = None) -> InlineKeyboardMarkup:
    """Кнопки под сводкой в личке: строка → выписка переписки с решением «снять нарушение»."""
    builder = InlineKeyboardBuilder()
    for label, callback in view.buttons:
        builder.button(text=label, callback_data=callback)
    builder.button(text="🔄 Обновить", callback_data=LabAction(kind=MANUAL_KIND).pack())
    if back_cb is not None:
        builder.button(text="‹ К отчётам", callback_data=back_cb)
    builder.adjust(1)
    return builder.as_markup()


def _as_time(text: str) -> _time:
    hours, minutes = str(text).split(":")
    return _time(int(hours), int(minutes))


def slot_times(digest_cfg: dict[str, Any]) -> list[str]:
    """Времена сводок из настройки — по возрастанию, без повторов.

    Мусор, попавший в базу мимо интерфейса, отбрасывается; пустой результат
    откатывается к умолчанию.
    """
    raw = digest_cfg.get("alerts_digest_times")
    if isinstance(raw, str):
        raw = raw.split(",")
    times: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = str(item).strip()
        try:
            _as_time(text)
        except (ValueError, AttributeError):
            continue
        if text not in times:
            times.append(text)
    times.sort()
    return times[:MAX_SLOTS] or list(DEFAULT_TIMES)


def normalize_slot(slot: str) -> str:
    """Именованный слот («morning») — в его время. Прочее — как есть."""
    return _LEGACY_SLOTS.get(slot, slot)


def _known_marks(state: dict[str, Any]) -> dict[str, Any]:
    marks = {
        _LEGACY_SLOTS[key]: value
        for key, value in (state or {}).items()
        if key in _LEGACY_SLOTS
    }
    marks.update(
        {key: value for key, value in (state or {}).items() if key not in _LEGACY_SLOTS}
    )
    return marks


def due_slot(
    now: datetime,
    calendar_cfg: dict[str, Any],
    state: dict[str, Any],
    times: list[str] | tuple[str, ...] | None = None,
) -> tuple[str | None, list[str]]:
    """(какой слот слать, какие пометить отработанными).

    Из нескольких наступивших слотов шлётся последний, остальные гасятся:
    перезапущенный воркер не вываливает три сводки подряд. В нерабочие дни сводок нет.
    """
    if not is_workday(now, calendar_cfg):
        return None, []
    slots = list(times or DEFAULT_TIMES)
    local = now.astimezone(calendar_tz(calendar_cfg))
    today = local.date().isoformat()
    marks = _known_marks(state)
    due = [
        slot
        for slot in slots
        if local.time() >= _as_time(slot) and marks.get(slot) != today
    ]
    if not due:
        return None, []
    return due[-1], due


def slot_title(slot: str, times: list[str] | tuple[str, ...]) -> str:
    if slot == MANUAL_SLOT:
        return "Сводка по алертам (по запросу)"
    if not times or len(times) == 1:
        return "Сводка по алертам"
    if slot == times[0]:
        return "Утренняя сводка по алертам"
    if slot == times[-1]:
        return "Вечерняя сводка по алертам"
    return "Дневная сводка по алертам"


def _parse_last_at(state: dict[str, Any]) -> datetime | None:
    """Момент предыдущего выпуска. None — выпусков ещё не было."""
    raw = (state or {}).get(LAST_AT_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


async def _load_state(session) -> dict[str, Any]:
    stored = await session.get(Setting, STATE_KEY)
    if stored is not None and isinstance(stored.value, dict):
        return dict(stored.value)
    return {}


async def _mark_slots(
    session, slots: list[str], today: str, *, sent_at: datetime | None = None
) -> None:
    """Отметить слоты отработанными; `sent_at` двигает границу «с прошлой сводки»."""
    stored = await session.get(Setting, STATE_KEY)
    marks: dict[str, Any] = {name: today for name in slots}
    if sent_at is not None:
        marks[LAST_AT_KEY] = sent_at.isoformat()
    if stored is None:
        session.add(Setting(key=STATE_KEY, value=marks))
    else:
        stored.value = {**(stored.value or {}), **marks}


async def suppress_passed_slots(session) -> list[str]:
    """Пометить сегодняшние прошедшие слоты отработанными. Возвращает какие.

    Зовётся при включении сводок и смене времён: иначе наступившее сегодня
    время выдало бы сводку в ту же минуту.
    """
    calendar_cfg = await get_section(session, "work_calendar")
    digest_cfg = await get_section(session, "digest")
    local = datetime.now(timezone.utc).astimezone(calendar_tz(calendar_cfg))
    passed = [
        slot for slot in slot_times(digest_cfg) if local.time() >= _as_time(slot)
    ]
    if passed:
        # Граница «с прошлой сводки» тоже сдвигается — начать со следующего срока.
        await _mark_slots(
            session, passed, local.date().isoformat(), sent_at=local.astimezone(timezone.utc)
        )
    return passed


async def claim_alert_digest(session) -> tuple[str, datetime | None] | None:
    """Забронировать выпуск короткой транзакцией, до сети.

    Возвращает (слот, момент предыдущего выпуска). Отметка двигается при
    бронировании, а не после отправки: два выпуска с одним окном повторили бы друг друга.
    """
    digest_cfg = await get_section(session, "digest")
    if not bool(digest_cfg.get("alerts_digest_enabled")):
        return None
    calendar_cfg = await get_section(session, "work_calendar")
    now = datetime.now(timezone.utc)
    state = await _load_state(session)
    slot, marks = due_slot(now, calendar_cfg, state, slot_times(digest_cfg))
    if slot is None:
        return None
    local_today = now.astimezone(calendar_tz(calendar_cfg)).date().isoformat()
    previous = _parse_last_at(state)
    await _mark_slots(session, marks, local_today, sent_at=now)
    return slot, previous


def _stamp(moment: datetime, tz) -> str:
    """«11:22, 25.12» — время впереди даты."""
    return f"{moment.astimezone(tz):%H:%M, %d.%m}"


def _pair(work: int | None, cal: int | None) -> str:
    if work is None or cal is None:
        return "время не посчитано"
    return f"за {fmt_duration(work)} рабочего времени (календарных {fmt_duration(cal)})"


_CLOSE_REASONS = {
    "dismissed": "закрыто вручную",
    "no_need": "закрыто принудительно: вердикт «ответа не требовалось»",
    "no_answer": "закрыто принудительно: перестали ждать",
    "gone": "данных об обращении больше нет",
}

_WORKED_REASONS = {
    KIND_NO_REACTION: "менеджер отработал",
    KIND_NO_SUBSTANTIVE: "специалист отработал",
}


def alert_outcome(
    kind: str, inter: Interaction | None, dismissed: bool = False
) -> str:
    """Исход алерта: closed / dismissed / no_answer / waiting / no_need / gone.

    Отдельно от рендера: исход решает, в какой блок сводки попадёт строка.
    """
    if inter is None:
        # Данных об обращении нет (чат удалён) — отдельный исход, чтобы строка не висела вечно.
        return "gone"
    if dismissed:
        return "dismissed"
    answered = (
        inter.first_reaction_at
        if kind == KIND_NO_REACTION
        else inter.substantive_at
    )
    if answered is not None:
        return "closed"
    if inter.state is InteractionState.ABANDONED:
        return "no_answer"
    # Важно: «висит» — это состояние обращения, а не «ответа не видно»: закрытое
    # вердиктом без ответа — не ожидание.
    if inter.state in (InteractionState.OPEN, InteractionState.REACTED):
        return "waiting"
    return "no_need"


def _alert_line(
    kind: str,
    title: str,
    inter: Interaction | None,
    staff_names: dict[int, str],
    calendar_cfg: dict[str, Any],
    now: datetime,
    dismissed: bool = False,
    show_kind: bool = True,
    dismissed_by: str | None = None,
) -> str:
    """Строка одного алерта в сводке; `show_kind=False` — ступень уже в заголовке блока."""
    head = f"«{escape(title)}»"
    if show_kind:
        head += f" · {_KIND_LABELS.get(kind, kind)}"

    # Знак исхода — в начале строки; зачёркивается только название.
    outcome = alert_outcome(kind, inter, dismissed)
    if outcome == "gone":
        return f"{head} — данных об обращении уже нет"
    if outcome == "dismissed":
        who = f": {escape(dismissed_by)}" if dismissed_by else ""
        return f"✋ <s>{head}</s> — ответ не требуется (закрыто вручную{who})"
    if outcome == "closed":
        if kind == KIND_NO_REACTION:
            staff_id = inter.first_reaction_staff_id
            work, cal = inter.ttfr_business_seconds, inter.ttfr_seconds
        else:
            staff_id = inter.substantive_staff_id
            work, cal = inter.ttfa_business_seconds, inter.ttfa_seconds
        who = staff_names.get(staff_id or 0)
        # Сотрудник не определён — ответ засчитан компании.
        answered = "компания ответила" if who is None else f"ответил(а) {escape(who)}"
        return f"✅ <s>{head}</s> — {answered} {_pair(work, cal)}"
    if outcome == "no_answer":
        return f"❓ {head} — закрыто без ответа: никто так и не ответил"
    if outcome == "no_need":
        # Бот закрыл обращение как не требующее ответа — алерт оказался ложным.
        return f"🤖 <s>{head}</s> — ответа не требовалось: вопроса не было"

    # Алерт означает, что порог уже сорван, — это горящая просрочка, а не ожидание.
    age_work = business_seconds(inter.opened_at, now, calendar_cfg)
    age_cal = int((now - inter.opened_at).total_seconds())
    return (
        f"🔥 {head} — без ответа уже {fmt_duration(age_work)} рабочего "
        f"времени (календарных {fmt_duration(age_cal)})"
    )


async def alert_digest_payload(
    session, slot: str, since: datetime | None = None, *, allow_empty: bool = False
) -> tuple[DigestView, list[int]] | None:
    """(сводка, адресаты). Плановый выпуск приходит и без изменений (`allow_empty`)."""
    from app.services.digest import _digest_targets

    digest_cfg = await get_section(session, "digest")
    recipients = await _digest_targets(session, digest_cfg)
    if not recipients:
        return None
    view = await build_alert_digest(session, slot, since=since)
    if view is None:
        times = slot_times(digest_cfg)
        if not allow_empty or normalize_slot(slot) not in times:
            return None
        # Плановый выпуск приходит и пустым, иначе отсутствие изменений
        # выглядит как сбой доставки.
        calendar_cfg = await get_section(session, "work_calendar")
        local = datetime.now(timezone.utc).astimezone(calendar_tz(calendar_cfg))
        view = DigestView(text=(
            f"🧾 <b>{slot_title(slot, times)}</b> · {local.strftime('%d.%m')}\n\n"
            "Алертов в ожидании ответа нет. "
            "С прошлого выпуска новых итогов по алертам нет."
        ))
    return view, recipients


async def build_alert_digest(
    session, slot: str, since: datetime | None = None
) -> DigestView | None:
    """Сводка слота: что висит сейчас и что закрылось. None — показывать нечего.

    `since` — момент предыдущего выпуска (из `claim_alert_digest`); None —
    выпусков ещё не было, окно начинается со вчерашней полуночи.
    """
    calendar_cfg = await get_section(session, "work_calendar")
    calendar = CalendarHistory(calendar_cfg)
    tz = calendar_tz(calendar_cfg)
    now = datetime.now(timezone.utc)
    local = now.astimezone(tz)
    today0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday0 = today0 - timedelta(days=1)

    times = slot_times(await get_section(session, "digest"))
    slot = normalize_slot(slot)

    # Окно закрытий — «с прошлой сводки до сейчас». Ручной вызов окно не двигает,
    # иначе «Обновить» гасила бы новости для плановой сводки.
    if since is None:
        since = _parse_last_at(await _load_state(session))
    win_start = since or yesterday0.astimezone(timezone.utc)
    win_end = now

    # «Когда перестали ждать» — то же правило, что у движка эпизодов,
    # с версиями порогов и окон на момент обращения.
    from app.services.episodes import alert_thresholds, expiry_moment, wait_windows
    from app.services.settings_store import versioned_fields
    from app.services.versioning import History

    alerts_cfg = await get_section(session, "alerts")
    alerts_history = History(alerts_cfg, versioned_fields("alerts"))
    episodes_history = History(
        await get_section(session, "episodes"), versioned_fields("episodes")
    )
    wait_reaction, wait_specialist = wait_windows(episodes_history.current)

    def died_at(inter: Interaction) -> datetime:
        sla, substantive = alert_thresholds(alerts_history.at(inter.opened_at))
        reaction, specialist = wait_windows(episodes_history.at(inter.opened_at))
        return expiry_moment(
            inter,
            calendar.at(inter.opened_at),
            sla_seconds=sla,
            substantive_seconds=substantive,
            wait_reaction_seconds=reaction,
            wait_specialist_seconds=specialist,
        )

    # Висящий кейс показывается независимо от даты алерта, поэтому глубина —
    # окно ожидания плюс запас; отбор висящих — в Python.
    lookback = timedelta(days=LOOKBACK_DAYS) + timedelta(
        seconds=max(wait_reaction, wait_specialist)
    )
    rows = (
        await session.execute(
            select(AlertLog, Interaction, Chat.title)
            .join(Chat, Chat.id == AlertLog.chat_id)
            .outerjoin(
                Interaction,
                (Interaction.chat_id == AlertLog.chat_id)
                & (Interaction.opened_by_message_id == AlertLog.opened_by_message_id),
            )
            .where(AlertLog.sent_at >= now - lookback)
            .where(AlertLog.shadow.is_(False))
            .order_by(AlertLog.sent_at)
        )
    ).all()
    # Поздний ответ после просрочки, по которой отдельный алерт не ушёл (например,
    # между тиками воркера), всё равно попадает в сводку синтетической строкой.
    # В очередь отправки такие строки не пишутся.
    existing = {
        (entry.chat_id, entry.opened_by_message_id, entry.kind) for entry, _, _ in rows
    }
    late = (
        await session.execute(
            select(Interaction, Chat.title)
            .join(Chat, Chat.id == Interaction.chat_id)
            .where(
                or_(
                    Interaction.sla_breached.is_(True)
                    & (Interaction.first_reaction_at >= win_start)
                    & (Interaction.first_reaction_at < win_end),
                    Interaction.substantive_breached.is_(True)
                    & (Interaction.substantive_at >= win_start)
                    & (Interaction.substantive_at < win_end),
                )
            )
        )
    ).all()
    for inter, title in late:
        case_rules = alerts_history.at(inter.opened_at)
        sla, specialist = alert_thresholds(case_rules)
        cfg = calendar.at(inter.opened_at)
        checks = [
            (KIND_NO_REACTION, inter.sla_breached, inter.first_reaction_at),
            (KIND_NO_SUBSTANTIVE, inter.substantive_breached, inter.substantive_at),
        ]
        for kind, breached, at in checks:
            key = inter.chat_id, inter.opened_by_message_id, kind
            if (
                not breached
                or at is None
                or not win_start <= at < win_end
                or key in existing
            ):
                continue
            if (
                kind == KIND_NO_SUBSTANTIVE
                and alerts_cfg.get("substantive_mode") != "on"
            ):
                continue
            if kind == KIND_NO_REACTION:
                deadline = response_deadline(inter.opened_at, sla, cfg)
            else:
                deadline = handoff_deadline(inter.handoff_at, specialist, cfg)
            if deadline is None:
                continue
            entry = SimpleNamespace(
                chat_id=inter.chat_id,
                opened_by_message_id=inter.opened_by_message_id,
                kind=kind,
                sent_at=deadline,
                summary_only=True,
            )
            rows.append((entry, inter, title))
            existing.add(key)
    if not rows:
        return None  # плановый выпуск получит текст об отсутствии алертов

    staff_ids = {
        sid
        for _, inter, _ in rows
        if inter is not None
        for sid in (inter.first_reaction_staff_id, inter.substantive_staff_id)
        if sid is not None
    }
    staff_names: dict[int, str] = {}
    if staff_ids:
        for person in (
            await session.scalars(select(Staff).where(Staff.id.in_(staff_ids)))
        ).all():
            staff_names[person.id] = person.full_name

    keys = {(entry.chat_id, entry.opened_by_message_id) for entry, _, _ in rows}
    dismissal_rows = (
        await session.execute(
            select(
                BreachDismissal.chat_id,
                BreachDismissal.opened_by_message_id,
                BreachDismissal.dismissed_at,
                BotUser.display_name,
                BotUser.username,
            )
            .outerjoin(BotUser, BotUser.id == BreachDismissal.dismissed_by)
            .where(BreachDismissal.chat_id.in_({chat_id for chat_id, _ in keys}))
        )
    ).all()
    dismissed_at = {
        (chat_id, message_id): moment
        for chat_id, message_id, moment, _name, _username in dismissal_rows
        if (chat_id, message_id) in keys
    }
    dismissed_by = {
        (chat_id, message_id): (name or username)
        for chat_id, message_id, _moment, name, username in dismissal_rows
        if (chat_id, message_id) in keys and (name or username)
    }
    dismissed_keys = set(dismissed_at)

    # Кейсы чатов, снятых с анализа, не висят.
    from app.services.tracking import currently_tracked_chats

    tracked = set(
        (await session.scalars(select(Chat.id).where(Chat.id.in_(currently_tracked_chats())))).all()
    )

    def happened_at(entry, inter, outcome, key) -> datetime:
        """Когда кейс перешёл в конечное состояние — по этому моменту строка попадает в выпуск.

        Для «вопроса не было» и «данных нет» берётся `struck_at` (момент, когда
        закрытие заметили; ставится один раз), без него — время алерта.
        """
        if outcome == "closed":
            return (
                inter.first_reaction_at
                if entry.kind == KIND_NO_REACTION
                else inter.substantive_at
            )
        if outcome == "dismissed":
            return dismissed_at.get(key) or entry.sent_at
        if outcome == "no_answer":
            return died_at(inter)
        # У синтетической строки (SimpleNamespace выше) поля struck_at нет.
        return getattr(entry, "struck_at", None) or entry.sent_at

    waiting: dict[str, list[tuple]] = {KIND_NO_REACTION: [], KIND_NO_SUBSTANTIVE: []}
    no_answer: list[tuple] = []
    closed: list[tuple] = []
    for entry, inter, title in rows:
        key = (entry.chat_id, entry.opened_by_message_id)
        dismissed = key in dismissed_keys
        outcome = alert_outcome(entry.kind, inter, dismissed)
        if outcome == "waiting" and entry.kind in waiting and entry.chat_id in tracked:
            waiting[entry.kind].append((entry, inter, title, dismissed, outcome, None))
            continue
        if outcome == "waiting":
            continue  # чат на паузе — не наше ожидание

        moment = happened_at(entry, inter, outcome, key)
        if not (moment is not None and win_start <= moment < win_end):
            continue  # закрылось не в этом окне — показано в своё время
        # Для «вопроса не было» и «данных нет» момент показа — не время закрытия,
        # поэтому причина в таких строках без времени.
        closed_at = moment if outcome in ("closed", "dismissed", "no_answer") else None
        item = (entry, inter, title, dismissed, outcome, closed_at)
        if outcome == "no_answer":
            no_answer.append(item)
        else:
            closed.append(item)

    hanging = waiting[KIND_NO_REACTION] + waiting[KIND_NO_SUBSTANTIVE]
    if not (hanging or no_answer or closed):
        return None

    lines: list[str] = []
    buttons: list[tuple[str, str]] = []
    counter = {"n": 0}

    def emit(item, *, with_button: bool, show_kind: bool) -> None:
        entry, inter, title, dismissed, outcome, closed_at = item
        rendered = _alert_line(
            entry.kind,
            title or "?",
            inter,
            staff_names,
            # Возраст — по графику на момент обращения, как в алерте и отчёте.
            calendar.at(inter.opened_at) if inter is not None else calendar_cfg,
            now,
            dismissed=dismissed,
            show_kind=show_kind,
            dismissed_by=dismissed_by.get(
                (entry.chat_id, entry.opened_by_message_id)
            ),
        )
        counter["n"] += 1
        number = counter["n"]
        if getattr(entry, "summary_only", False):
            note = f"срок истёк в {_stamp(entry.sent_at, tz)}; ответ с опозданием"
        else:
            note = f"алерт сработал в {_stamp(entry.sent_at, tz)}"
        reason = _CLOSE_REASONS.get(outcome) or (
            _WORKED_REASONS.get(entry.kind) if outcome == "closed" else None
        )
        # Имя снявшего уже в строке — в приписке не повторяется.
        if reason:
            when = f" в {_stamp(closed_at, tz)}" if closed_at else ""
            note += f" · закрыт{when} — {reason}"
        lines.append(f"{number}. {rendered}\n    <i>{note}</i>")
        if with_button and len(buttons) < MAX_BUTTONS:
            buttons.append(
                (
                    f"💬 {number}. {(title or '?')[:28]}",
                    AlertAction(
                        action="ctxd",
                        chat_id=entry.chat_id,
                        msg_id=entry.opened_by_message_id,
                    ).pack(),
                )
            )

    def block(title: str, items: list, *, with_button: bool, show_kind: bool) -> None:
        if not items:
            return
        lines.append("")
        lines.append(title)
        for index, item in enumerate(items[:MAX_PER_BLOCK]):
            if index:
                lines.append("")
            emit(item, with_button=with_button, show_kind=show_kind)
        if len(items) > MAX_PER_BLOCK:
            lines.append(f"    <i>…и ещё {len(items) - MAX_PER_BLOCK}</i>")

    # Кружки те же, что в алертах: 🔴 первая ступень, 🟠 вторая.
    block(
        "🔴 <b>Нет реакции менеджера</b>",
        waiting[KIND_NO_REACTION],
        with_button=True,
        show_kind=False,
    )
    block(
        "🟠 <b>Нет ответа специалиста</b>",
        waiting[KIND_NO_SUBSTANTIVE],
        with_button=True,
        show_kind=False,
    )
    block(
        "❓ <b>Остались без ответа</b> — ждать перестали",
        no_answer,
        with_button=True,
        show_kind=True,
    )
    block("🟢 <b>Закрыто</b>", closed, with_button=False, show_kind=True)

    tally = {name: 0 for name in ("closed", "dismissed", "no_need", "gone")}
    for item in closed:
        tally[item[4]] = tally.get(item[4], 0) + 1
    summary = f"Ждут ответа: {len(hanging)}"
    if no_answer:
        summary += f" · без ответа: {len(no_answer)}"
    summary += f" · закрыто: {tally['closed']}"
    if tally["dismissed"]:
        summary += f" · снято решением: {tally['dismissed']}"
    if tally["no_need"]:
        summary += f" · ответа не требовалось: {tally['no_need']}"

    text = (
        f"🧾 <b>{slot_title(slot, times)}</b>\n{summary}\n"
        + "\n".join(lines)
        + "\n\n<i>Время — от сообщения клиента до ответа. Кнопка с номером "
        "(в личке) — переписка и решение «снять нарушение».</i>"
    )
    return DigestView(text=text, buttons=buttons)


async def maybe_send_alert_digest(bot) -> bool:
    """Тик воркера: пора — собрать и разослать. Бронь и сборка в транзакциях, сеть вне их."""
    from app.db.base import session_scope

    async with session_scope() as session:
        claim = await claim_alert_digest(session)
    if claim is None:
        return False
    slot, since = claim

    async with session_scope() as session:
        payload = await alert_digest_payload(session, slot, since, allow_empty=True)
    if payload is None:
        return False

    view, recipients = payload
    delivered = 0
    for tg_id in recipients:
        try:
            # Группа уведомлений (отрицательный id) — без кнопок: бот там только публикует.
            await bot.send_message(
                tg_id,
                view.text,
                parse_mode="HTML",
                reply_markup=digest_keyboard(view) if tg_id > 0 else None,
            )
            delivered += 1
        except Exception:  # noqa: BLE001 — недоставленное не роняет тик
            log.exception("alert_digest.send_failed", tg_id=tg_id)

    log.info("alert_digest.sent", slot=slot, delivered=delivered)
    return delivered > 0
