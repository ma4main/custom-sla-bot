"""Раздел «Отчёты»: нагрузка по чатам и сотрудникам, выгрузка XLSX/CSV/HTML.

Период и охват применяются к самим запросам, а не печатаются поверх общих
цифр. Каждый отчёт показывает качество данных — долю исходящих без автора.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from html import unescape
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton
from aiogram.types import Message as TgMessage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from app.bot.callbacks import (
    DRILL_FROM_ATTENTION,
    AlertAction,
    DismissedAction,
    DrillAction,
    LabAction,
    Nav,
    ReportAction,
)
from app.bot.keyboards import _pagination, back_to, reports_menu
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import BotUser, Chat, ChatState, Staff
from app.services.access import Perm, has_perm
from app.services.calendar import business_seconds
from app.services.dismissals import PER_PAGE as DISMISSED_PER_PAGE
from app.services.dismissals import dismissed_page
from app.services.export import build_export
from app.services.report_data import (
    load_chat_report,
    load_speed,
    load_staff_report,
    load_summary,
)
from app.services.report_drill import (
    KIND_BREACH,
    KIND_BREACH_REACTION,
    KIND_BREACH_SPECIALIST,
    KIND_HANDOFF,
    KIND_LABELS,
    KIND_NO_ANSWER,
    KIND_NO_NEED,
    KIND_WAITING,
    drill_counts,
    drill_page,
)
from app.services.report_drill import PER_PAGE as DRILL_PER_PAGE
from app.services.settings_store import get_section
from app.services.transcript import (
    calendar_tz,
    fmt_duration,
    fmt_when,
    specialist_deadline_label,
)
from app.text import esc

router = Router(name="reports")
# Только приватные диалоги: в группы бот не пишет и меню там не показывает.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

PER_PAGE = 8
TOP_LIMIT = 8  # строк в telegram-сообщении; полный список — в файле

PERIOD_LABELS = {
    "today": "сегодня",
    "yesterday": "вчера",
    "last7": "последние 7 дней (по вчера)",
    "prev_week": "прошлая неделя (пн–вс)",
    "this_month": "этот месяц",
    "prev_month": "прошлый месяц",
}

# Произвольный период кодируется в callback data («c20260801-20260815»):
# кнопки выгрузки работают без FSM, которое к моменту нажатия уже очищено.
_CUSTOM_PERIOD = re.compile(r"^c(\d{8})-(\d{8})$")


class ReportPeriodForm(StatesGroup):
    waiting_range = State()


class DismissedPeriodForm(StatesGroup):
    """Свой ввод периода для журнала снятий: в состоянии отчётов лежат охват
    и цель, которых здесь нет."""

    waiting_range = State()


def period_label(period: str) -> str:
    match = _CUSTOM_PERIOD.match(period or "")
    if match is None:
        return PERIOD_LABELS.get(period, period)
    first = datetime.strptime(match.group(1), "%Y%m%d").date()
    last = datetime.strptime(match.group(2), "%Y%m%d").date()
    if first == last:
        return first.strftime("%d.%m.%Y")
    return f"{first.strftime('%d.%m.%Y')} — {last.strftime('%d.%m.%Y')}"


SCOPE_LABELS = {
    "all": "Сводный по всем чатам",
    "chat": "По чату",
    "staff": "По сотруднику",
    "self": "Мои показатели",
}

# Право проверяется при каждом запуске: callback data — не доверенный вход,
# старые кнопки после понижения роли не открывают чужие данные.
SCOPE_PERMS = {
    "all": Perm.REPORT_ALL_CHATS,
    "chat": Perm.REPORT_CHAT,
    "staff": Perm.REPORT_ANY_STAFF,
    "self": Perm.REPORT_SELF,
}


def _cut(text: str | None, limit: int) -> str:
    """Сначала обрезать, потом экранировать: иначе «&amp;» режется пополам."""
    return esc((text or "?")[:limit])


def period_bounds(period: str) -> tuple[datetime, datetime]:
    """Границы периода в UTC, посчитанные в рабочем часовом поясе."""
    tz = ZoneInfo(get_settings().tz)
    now = datetime.now(tz)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "today":
        start, end = today0, now
    elif period == "yesterday":
        start, end = today0 - timedelta(days=1), today0
    elif period == "last7":
        # Без сегодняшнего дня: семь завершённых дней — цифры не плывут в течение дня.
        start, end = today0 - timedelta(days=7), today0
    elif period == "prev_week":
        start = today0 - timedelta(days=today0.weekday() + 7)
        end = start + timedelta(days=7)
    elif period == "this_month":
        start, end = today0.replace(day=1), now
    elif period == "prev_month":
        first_this = today0.replace(day=1)
        start = (first_this - timedelta(days=1)).replace(day=1)
        end = first_this
    elif (match := _CUSTOM_PERIOD.match(period)) is not None:
        first = datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=tz)
        last = datetime.strptime(match.group(2), "%Y%m%d").replace(tzinfo=tz)
        # Обе даты включительно: конец — полночь следующего дня.
        start, end = first, last + timedelta(days=1)
    else:
        raise ValueError(f"Неизвестный период: {period}")

    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _periods_kb(scope: str, target_id: int = 0):
    builder = InlineKeyboardBuilder()
    for key, label in PERIOD_LABELS.items():
        builder.button(
            text=label.capitalize(),
            callback_data=ReportAction(
                action="run", scope=scope, period=key, target_id=target_id
            ).pack(),
        )
    builder.button(
        text="📅 Произвольный период",
        callback_data=ReportAction(action="custom", scope=scope, target_id=target_id).pack(),
    )
    builder.button(text="‹ Назад", callback_data=Nav(to="reports").pack())
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def _result_kb(
    scope: str,
    period: str,
    target_id: int,
    can_export: bool,
    drill: dict[str, int] | None = None,
):
    builder = InlineKeyboardBuilder()
    sizes: list[int] = []
    if can_export:
        for fmt, label in (("expx", "📥 XLSX"), ("expc", "📥 CSV")):
            builder.button(
                text=label,
                callback_data=ReportAction(
                    action=fmt, scope=scope, period=period, target_id=target_id
                ).pack(),
            )
        export_row = 2
        if scope == "all":
            # HTML-страница — только у сводного отчёта; период наследуется,
            # включая произвольный.
            builder.button(
                text="📄 HTML",
                callback_data=ReportAction(
                    action="exph", scope=scope, period=period, target_id=target_id
                ).pack(),
            )
            export_row = 3
        sizes.append(export_row)
    # Проваливание — только в ненулевые срезы.
    if drill:
        drill_chat = target_id if scope == "chat" else 0
        # «Ответ не требовался» кнопкой не показывается: действия по этому списку нет.
        for kind in (
            KIND_WAITING,
            KIND_BREACH_REACTION,
            KIND_BREACH_SPECIALIST,
            KIND_HANDOFF,
            KIND_NO_ANSWER,
        ):
            count = drill.get(kind) or 0
            if count:
                builder.button(
                    text=f"{KIND_LABELS[kind]}: {count}",
                    callback_data=DrillAction(
                        kind=kind, period=period, chat_id=drill_chat
                    ).pack(),
                )
                sizes.append(1)
    builder.button(text="‹ К отчётам", callback_data=Nav(to="reports").pack())
    sizes.append(1)
    builder.adjust(*sizes)
    return builder.as_markup()


# ── Навигация ─────────────────────────────────────────────────


@router.callback_query(Nav.filter(F.to == "reports"))
async def on_reports(query: CallbackQuery, state: FSMContext, bot_user: BotUser) -> None:
    # «Назад» из ввода произвольного периода — снять ожидание текста,
    # иначе следующее сообщение утечёт в брошенную форму.
    await state.clear()
    await query.message.edit_text(
        "<b>📊 Отчёты</b>\n\n"
        "Выберите разрез.\n"
        "Дальше — период.\n\n"
        "<i>«Последние 7 дней» — скользящее окно: семь полных дней по вчера, "
        "сегодняшний не считается.\n"
        "«Прошлая неделя» — календарная, с понедельника по воскресенье.\n"
        "Числа у них будут разными — это не ошибка.</i>",
        reply_markup=reports_menu(bot_user),
        parse_mode="HTML",
    )
    await query.answer()


@router.callback_query(ReportAction.filter(F.action == "scope"))
async def on_scope(query: CallbackQuery, callback_data: ReportAction, bot_user: BotUser) -> None:
    scope = callback_data.scope or ""
    if not has_perm(bot_user, SCOPE_PERMS.get(scope, "")):
        await query.answer("Недостаточно прав", show_alert=True)
        return

    if scope in ("chat", "staff"):
        await _render_picker(query, scope, page=0)
        return

    if scope == "self" and bot_user.staff_id is None:
        # Проверка привязки — до выбора периода: иначе период спрашивается зря.
        await query.message.edit_text(
            "<b>🙋 Мои показатели</b>\n\n"
            "Ваша учётная запись пока не привязана к сотруднику из справочника, "
            "поэтому персональных показателей нет.\n\n"
            "<i>Привязку делает владелец: «Пользователи бота» → ваша "
            "карточка → «🔗 Привязать к сотруднику».</i>",
            reply_markup=back_to("reports"),
            parse_mode="HTML",
        )
        await query.answer()
        return

    await query.message.edit_text(
        f"<b>{SCOPE_LABELS[scope]}</b>\n\nВыберите период.",
        reply_markup=_periods_kb(scope),
        parse_mode="HTML",
    )
    await query.answer()


async def _render_picker(query: CallbackQuery, scope: str, page: int) -> None:
    async with session_scope() as session:
        if scope == "chat":
            total = await session.scalar(
                select(func.count(Chat.id)).where(Chat.state == ChatState.TRACKED)
            ) or 0
            rows = (
                await session.scalars(
                    select(Chat)
                    .where(Chat.state == ChatState.TRACKED)
                    .order_by(Chat.title.nulls_last())
                    .offset(page * PER_PAGE)
                    .limit(PER_PAGE)
                )
            ).all()
            items = [(chat.id, chat.title or str(chat.tg_chat_id)) for chat in rows]
            header = "Выберите чат"
        else:
            total = await session.scalar(
                select(func.count(Staff.id)).where(Staff.active.is_(True))
            ) or 0
            rows = (
                await session.scalars(
                    select(Staff)
                    .where(Staff.active.is_(True))
                    .order_by(Staff.full_name)
                    .offset(page * PER_PAGE)
                    .limit(PER_PAGE)
                )
            ).all()
            items = [(person.id, person.full_name) for person in rows]
            header = "Выберите сотрудника"

    builder = InlineKeyboardBuilder()
    for target_id, label in items:
        builder.button(
            text=label[:60],
            callback_data=ReportAction(action="target", scope=scope, target_id=target_id).pack(),
        )
    builder.adjust(1)
    # Стрелки и счётчик «N/M» — одной строкой; счётчик не нажимается.
    _pagination(
        builder,
        lambda page, **kw: ReportAction(action="pick", scope=scope, target_id=page),
        page=page,
        total=total,
        per_page=PER_PAGE,
    )

    # Отчёты «про всех сотрудников сразу» — здесь, а не в корне меню.
    if scope == "staff":
        builder.row(
            InlineKeyboardButton(
                text="⏱ Скорость по всем сотрудникам",
                callback_data=LabAction(kind="speed").pack(),
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="🌙 Работа вне графика",
                callback_data=LabAction(kind="night").pack(),
            )
        )

    builder.row(InlineKeyboardButton(text="‹ Назад", callback_data=Nav(to="reports").pack()))

    text = f"<b>{SCOPE_LABELS[scope]}</b>\n\n{header}."
    if scope == "staff":
        text += "\n\n<i>Ниже — сводные отчёты сразу по всем сотрудникам.</i>"
    if not items:
        text += "\n\nПока пусто."
    await query.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await query.answer()


@router.callback_query(ReportAction.filter(F.action == "pick"))
async def on_pick_page(query: CallbackQuery, callback_data: ReportAction, bot_user: BotUser) -> None:
    scope = callback_data.scope or ""
    if not has_perm(bot_user, SCOPE_PERMS.get(scope, "")):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    await _render_picker(query, scope, page=callback_data.target_id)


@router.callback_query(ReportAction.filter(F.action == "target"))
async def on_target(query: CallbackQuery, callback_data: ReportAction, bot_user: BotUser) -> None:
    scope = callback_data.scope or ""
    if not has_perm(bot_user, SCOPE_PERMS.get(scope, "")):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    await query.message.edit_text(
        f"<b>{SCOPE_LABELS[scope]}</b>\n\nВыберите период.",
        reply_markup=_periods_kb(scope, callback_data.target_id),
        parse_mode="HTML",
    )
    await query.answer()


# ── Построение отчёта ─────────────────────────────────────────


def _quality_line(unresolved: int, pct: float) -> str:
    if not unresolved:
        return "✅ Автор определён у всех исходящих."
    mark = "⚠️ " if pct > 5 else ""
    return (
        f"{mark}Исходящих без автора: {unresolved} ({pct}%) — "
        "разметка в «Сотрудники → 🔍 Не определили, кто это»."
    )


# Предел Telegram — 4096 символов видимого текста (после разбора HTML).
# Заголовок и подпись рассылки добавляются снаружи, поэтому списки ужимаются
# ступенями, пока тело не влезет; полные списки — в HTML-файле.
TELEGRAM_LIMIT = 4096
SUMMARY_BUDGET = 3700
# Ступени лимитов: (чатов, сотрудников, чатов в строке просрочек сотрудника).
_LIST_LIMITS = ((TOP_LIMIT, TOP_LIMIT, 6), (5, 5, 3), (3, 3, 2), (0, 0, 1))
HTML_NOTE = (
    "📄 Полные списки чатов и сотрудников — в HTML-файле: кнопка «📄 HTML» "
    "под отчётом или вложение к рассылке."
)


def visible_length(text: str) -> int:
    """Длина так, как её считает Telegram: без тегов, сущности — одним символом."""
    return len(unescape(re.sub(r"<[^>]+>", "", text)))


async def _render_all(session, start, end, layout: str = "full") -> str:
    """Сводный отчёт. `layout="brief"` — только цифры, без списков (рассылки).

    Данные грузятся один раз, текст собирается чистой функцией — при переборе
    длины он пересобирается с меньшими списками.
    """
    data = await load_summary(session, start, end)
    if data["tracked"] == 0:
        return "⚠️ Ни один чат не включён в анализ."
    brief = layout == "brief"

    breaches: dict = {}
    if data["staff"] and not brief:
        # Просрочки по сотрудникам и в каких чатах — прямо в сводке.
        from app.services.report_lab import staff_breaches

        breaches = await staff_breaches(session, start, end)
    speed = await load_speed(session, start, end)

    # Сноска о смене графика внутри периода: цифры посчитаны по двум календарям.
    from app.services.calendar import CalendarHistory

    note = CalendarHistory(await get_section(session, "work_calendar")).footnote(
        start, end
    )

    text = ""
    for chat_top, staff_top, breach_chats in _LIST_LIMITS:
        text = _compose_summary(
            data, breaches, speed, note, brief,
            chat_top=chat_top, staff_top=staff_top, breach_chats=breach_chats,
        )
        if visible_length(text) <= SUMMARY_BUDGET:
            break
    return text


def _compose_summary(
    data: dict,
    breaches: dict,
    speed: dict,
    note: str | None,
    brief: bool,
    *,
    chat_top: int,
    staff_top: int,
    breach_chats: int,
) -> str:
    cut = False
    lines = [
        f"Чатов в анализе за период: <b>{data['tracked']}</b>",
        f"Сообщений: <b>{data['incoming'] + data['outgoing']}</b> — "
        f"от клиентов {data['incoming']}, от компании {data['outgoing']}",
        "",
    ]
    active_chats = [c for c in data["chats"] if c["incoming"] + c["outgoing"]]
    if brief and active_chats:
        # В кратком составе списков нет, но объём активности виден.
        lines.append(f"Активных чатов за период: {len(active_chats)}")
        lines.append("")
    if active_chats and not brief:
        lines.append(f"<b>Чаты</b> — активных за период: {len(active_chats)}")
        for item in active_chats[:chat_top]:
            lines.append(
                f"• {_cut(item['title'], 40)} — "
                f"вх {item['incoming']} / исх {item['outgoing']}"
            )
        if len(active_chats) > chat_top:
            cut = True
            shown = min(chat_top, len(active_chats))
            lines.append(
                f"<i>Показаны {shown} самых нагруженных из {len(active_chats)}.</i>"
                if shown
                else "<i>Список не поместился в сообщение.</i>"
            )
        lines.append("")
    if data["staff"] and not brief:
        lines.append("<b>Сотрудники</b>")
        for item in data["staff"][:staff_top]:
            lines.append(
                f"• {esc(item['full_name'])} — {item['messages']} сообщ., "
                f"{item['chats_touched']} чат(а), активных дней: {item['active_days']}"
            )
            hit = breaches.get(item["id"])
            if hit and hit["count"]:
                ranked = sorted(hit["chats"].items(), key=lambda kv: -kv[1])
                chats = ", ".join(
                    f"{_cut(title, 32)}" + (f" ×{n}" if n > 1 else "")
                    for title, n in ranked[:breach_chats]
                )
                if len(ranked) > breach_chats:
                    cut = True
                    chats += f" и ещё {len(ranked) - breach_chats}"
                # Доля — от обращений, которые вёл сам сотрудник.
                kinds = []
                if hit["reaction"]:
                    kinds.append(f"реакция {hit['reaction']}")
                if hit["specialist"]:
                    kinds.append(f"специалист {hit['specialist']}")
                lines.append(
                    f"    🔴 просрочено: {hit['count']} из {hit['handled']} обращ. "
                    f"({hit['share']}%; {', '.join(kinds)}) — {chats}"
                )
        if len(data["staff"]) > staff_top:
            cut = True
            lines.append(f"<i>…и ещё {len(data['staff']) - staff_top}.</i>")
        shown_hits = [breaches.get(item["id"]) for item in data["staff"][:staff_top]]
        if any(hit and hit["count"] for hit in shown_hits):
            lines.append(
                "<i>🔴 — просрочки за период (реакция позже порога или ответ "
                "специалиста позже срока), засчитаны тому, кто отвечал; "
                "доля — от обращений, которые вёл сотрудник.</i>"
            )
        lines.append("")
    if speed["total"]:
        lines.append("<b>Скорость (рабочее время)</b>")
        # Сумма состояний сходится с общим числом; «ответ не требовался» —
        # в скобке к итогу, но из общего числа вычитается.
        answerable = speed["total"] - speed["no_response"]
        lines.append(
            f"Обращений: <b>{speed['total']}</b>"
            + (
                f" (из них {speed['no_response']} ответа не требовали)"
                if speed["no_response"]
                else ""
            )
        )
        lines.append(f"Требовали ответа: <b>{answerable}</b>")
        lines.append(f"• отвечено: {speed['answered']}")
        waiting_line = f"• ждут ответа: {speed['waiting']}"
        if speed["waiting_paused"]:
            # Кнопки-срезы паузу не учитывают, а это число — учитывает.
            waiting_line += f" (из них в чатах на паузе: {speed['waiting_paused']})"
        lines.append(waiting_line)
        lines.append(f"• остались без ответа: {speed['timed_out']}")
        # Обращение ≠ сообщение: три сообщения подряд про одно — одно обращение.
        lines.append(
            "<i>Обращение — это вопрос клиента, а не сообщение: несколько "
            "сообщений подряд про одно и то же считаются одним обращением. "
            f"Поэтому {data['incoming']} сообщений от клиентов дали "
            f"{speed['total']} обращений.</i>"
        )
        if speed["timed_out"]:
            lines.append("")
            lines.append(
                "<i>«Остались без ответа» — клиент написал, а ответа "
                "по существу так и не было.\n"
                f"Бот ждёт {speed['wait_reaction_hours']} ч после срока "
                f"реакции — или {speed['wait_specialist_days']} дн после "
                "срока специалиста, если вопрос ему передавали. Потом "
                "перестаёт ждать и показывает их здесь, чтобы не потерялись.\n"
                "Конкретные чаты — кнопкой ниже.</i>"
            )
        lines.append("")
        # Ступени — независимые блоки с разными знаменателями: у менеджеров —
        # все обращения, требующие ответа, у специалистов — только переданные.
        lines.append("👤 <b>Менеджеры — первая реакция</b>")
        lines.append(f"Порог: {speed['reaction_limit_min']} мин")
        lines.append(
            f"Обычно {_fmt_secs(speed['ttfr_median'])}, "
            f"9 из 10 — быстрее, чем {_fmt_secs(speed['ttfr_p90'])}"
        )
        lines.append(
            f"Просрочено: {speed['breach_reaction']} из {answerable} обращений"
        )
        lines.append("")
        # Просрочки специалистов считаются от передачи.
        lines.append("🛠 <b>Специалисты — ответ после передачи</b>")
        lines.append(f"Срок: {specialist_deadline_label(speed['substantive_limit_min'])}")
        lines.append(
            f"Обычно {_fmt_secs(speed['ttfa_median'])}, "
            f"9 из 10 — быстрее, чем {_fmt_secs(speed['ttfa_p90'])}"
        )
        lines.append(
            f"Просрочено: {speed['breach_substantive']} "
            f"из {speed['handoffs']} переданных"
        )
        lines.append("")
        lines.append(
            "<i>«Обычно» — столько ждёт типичный клиент: половина обращений "
            "разобрана быстрее, половина дольше.\n"
            "«9 из 10 — быстрее, чем» — девять обращений из десяти уложились "
            "в это время, и только одно из десяти ждало дольше.\n"
            "Ступень — про сообщение, а не про должность: первым отреагировать "
            "может и специалист. Одно обращение может быть просрочено "
            "на обеих ступенях.</i>"
        )
        lines.append("")
    lines.append(_quality_line(data["unresolved"], data["unresolved_pct"]))

    if note:
        lines.append("")
        lines.append(f"<i>🕐 {esc(note)}</i>")
    if cut:
        lines.append("")
        lines.append(f"<i>{HTML_NOTE}</i>")
    return "\n".join(lines)


def _fmt_secs(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    # Одна реализация на весь бот: «меньше минуты» вместо «0 минут».
    return fmt_duration(seconds)


async def _render_chat(session, chat_id, start, end) -> str:
    data = await load_chat_report(session, chat_id, start, end)
    if data is None:
        return "Чат не найден."

    lines = [
        f"<b>{esc(data['title'])}</b>",
        "",
        f"От клиента: <b>{data['incoming']}</b> ({data['client_chars']} симв.)",
        f"От компании: <b>{data['outgoing']}</b> ({data['company_chars']} симв.)",
    ]
    if data["staff"]:
        lines.append("")
        lines.append("<b>Кто отвечал</b>")
        for item in data["staff"][:TOP_LIMIT]:
            lines.append(f"• {esc(item['full_name'])} — {item['messages']} сообщ.")
    if data["tracked_since"] and data["tracked_since"] > start:
        tz = calendar_tz(await get_section(session, "work_calendar"))
        since = data["tracked_since"].astimezone(tz)
        lines.append("")
        lines.append(
            f"<i>Чат в анализе с {since.strftime('%d.%m %H:%M')} — "
            "часть периода не наблюдалась.</i>"
        )
    return "\n".join(lines)


async def _render_staff(session, staff_id, start, end) -> str:
    data = await load_staff_report(session, staff_id, start, end)
    if data is None:
        return "Сотрудник не найден."

    lines = [
        f"<b>{esc(data['full_name'])}</b>"
        + ("" if data["active"] else " <i>(больше не работает)</i>"),
        "",
        f"Сообщений: <b>{data['messages']}</b> ({data['chars']} символов)",
        f"Чатов: {len(data['chats'])}",
    ]
    # «Активных дней» — только там, где число что-то значит. Знаменатель —
    # по календарным датам; конец полуоткрытого интервала сдвигается на секунду
    # назад, чтобы полночь не добавляла лишний день.
    tz = ZoneInfo(get_settings().tz)
    period_days = (
        (end - timedelta(seconds=1)).astimezone(tz).date()
        - start.astimezone(tz).date()
    ).days + 1
    if data["active_days"] and period_days > 1:
        lines.append(f"Дней с активностью: {data['active_days']} из {period_days}")

    if data["reactions"] or data["answered"]:
        lines.append("")
        lines.append("<b>Работа с обращениями</b>")
        if data["reactions"]:
            lines.append(
                f"Отреагировал(а) первым: {data['reactions']} — "
                f"обычно за {_fmt_secs(data['reaction_median'])}, "
                f"9 из 10 — быстрее, чем {_fmt_secs(data['reaction_p90'])}"
            )
        if data["answered"]:
            lines.append(f"Закрыл(а) по существу: {data['answered']}")
        if data["breached"]:
            lines.append(f"⚠️ Из них с опозданием: {data['breached']}")

    if data["chats"]:
        lines.append("")
        lines.append("<b>По чатам</b>")
        for item in data["chats"][:TOP_LIMIT]:
            lines.append(f"• {_cut(item['title'], 40)} — {item['messages']} сообщ.")
        if len(data["chats"]) > TOP_LIMIT:
            lines.append(
                f"<i>Показаны {TOP_LIMIT} самых активных из {len(data['chats'])}.</i>"
            )
    else:
        lines.append("")
        lines.append("<i>В этом периоде сообщений не найдено.</i>")
    return "\n".join(lines)


async def _render_self(session, bot_user: BotUser, start, end) -> str:
    if bot_user.staff_id is None:
        return (
            "Ваша учётная запись не привязана к сотруднику из справочника — "
            "персональные показатели считать не по чему. Привязку делает владелец."
        )
    return await _render_staff(session, bot_user.staff_id, start, end)


async def _build_report(
    bot_user: BotUser, scope: str, target_id: int, start: datetime, end: datetime
) -> str:
    async with session_scope() as session:
        if scope == "all":
            return await _render_all(session, start, end)
        if scope == "chat":
            return await _render_chat(session, target_id, start, end)
        if scope == "staff":
            return await _render_staff(session, target_id, start, end)
        return await _render_self(session, bot_user, start, end)


async def _drill_counts_for(
    scope: str, target_id: int, start: datetime, end: datetime
) -> dict[str, int] | None:
    """Числа критических срезов для кнопок. None — срезы к разрезу неприменимы."""
    if scope not in ("all", "chat"):
        return None
    async with session_scope() as session:
        return await drill_counts(
            session, start, end, chat_id=target_id if scope == "chat" else 0
        )


# ── Проваливание из числа в список обращений ──────────────────

_DRILL_HINTS = {
    KIND_BREACH: (
        "Срок ответа нарушен: либо не отреагировали вовремя, "
        "либо специалист не ответил после передачи."
    ),
    KIND_BREACH_REACTION: (
        "Клиенту ответили, но позже срока.\n"
        "Отвечать мог кто угодно из компании — важен сам факт отклика.\n"
        "Тех, кому не ответили до сих пор, смотрите в «⌛ Ответа так и нет»."
    ),
    KIND_WAITING: (
        "Клиент написал, и ответа по существу до сих пор нет.\n"
        "Сверху — кто ждёт дольше всех.\n"
        "Передачи специалисту по ним не было: как только она случится, "
        "обращение переедет в «⏳ Ждут ответа специалиста»."
    ),
    KIND_BREACH_SPECIALIST: (
        "Менеджер передал вопрос специалисту, и тот ответил позже срока.\n"
        "Срок здесь считается от передачи, а не от обращения клиента."
    ),
    KIND_HANDOFF: (
        "Менеджер передал вопрос специалисту, ответа ещё нет.\n"
        "Сверху — кто ждёт дольше всех."
    ),
    KIND_NO_ANSWER: (
        "Клиент написал, а ответа по существу так и не было.\n"
        "Бот перестал их ждать по пределу времени — стоит пройтись руками."
    ),
    KIND_NO_NEED: (
        "Бот решил, что ответа не ждут.\n"
        "«Спасибо», «ок», «+» и очень короткое — по правилу; остальное — "
        "модель по смыслу.\n"
        "В строке видно, кто решил и почему: проверяйте выборочно."
    ),
}

# Метки модели — по-русски: «info» читателю отчёта ни о чём не говорит.
_VERDICT_LABELS = {
    "info": "информация без вопроса",
    "ack": "подтверждение",
    "social": "вежливость",
    "offline": "просьба созвониться — ответ вне чата",
    # Три различимых случая: в отчёте они не должны сливаться в одну формулировку.
    "answer": "ответ на наш вопрос",
    "addition": "дополнение к открытому обращению",
    "correction": "поправка к открытому обращению",
}


def _drill_row(item: dict, n: int, tz, now: datetime, calendar_cfg: dict, kind: str) -> str:
    """Одна строка списка: чат, время, сколько ждёт, кто отвечал.

    `calendar_cfg` — настройка целиком: ожидание считается по графику,
    действовавшему в момент обращения.
    """
    from app.services.calendar import calendar_at

    calendar_cfg = calendar_at(calendar_cfg, item.get("opened_at"))
    title = _cut(item["title"], 38)
    if kind == KIND_HANDOFF:
        waited = business_seconds(item["handoff_at"], now, calendar_cfg)
        who = (
            f"передал(а) {esc(item['staff_name'])}"
            if item["staff_name"]
            else "автор передачи не распознан"
        )
        detail = (
            f"передача {fmt_when(item['handoff_at'], tz, now)} · "
            f"ждёт {fmt_duration(waited)} · {who}"
        )
    elif kind == KIND_WAITING:
        waited = business_seconds(item["opened_at"], now, calendar_cfg)
        who = (
            f"откликнулся(ась) {esc(item['staff_name'])}, ответа по существу нет"
            if item["first_reaction_at"]
            else "никто не откликнулся"
        )
        detail = (
            f"обращение {fmt_when(item['opened_at'], tz, now)} · "
            f"ждёт {fmt_duration(waited)} · {who}"
        )
    elif kind in (KIND_BREACH, KIND_BREACH_REACTION, KIND_BREACH_SPECIALIST):
        missed = [
            label
            for flag, label in (
                (item["sla_breached"], "реакция"),
                (item["substantive_breached"], "ответ специалиста"),
            )
            if flag
        ]
        who = (
            f"отвечал(а) {esc(item['staff_name'])}"
            if item["staff_name"]
            else ("реакции не было" if item["first_reaction_at"] is None else "автор не распознан")
        )
        detail = (
            f"обращение {fmt_when(item['opened_at'], tz, now)} · "
            f"просрочено: {', '.join(missed)} · {who}"
        )
    elif kind == KIND_NO_NEED:
        quote = (item["opener_text"] or "").strip().replace("\n", " ")[:60]
        if item["verdict_source"] == "rule":
            why = "правило («спасибо»/«ок»/короткое)"
        elif item["verdict_source"] == "model":
            label = _VERDICT_LABELS.get(item["verdict_label"] or "", item["verdict_label"])
            why = f"модель: {label}" if label else "модель"
        else:
            why = "вердикт не найден"
        series = (
            f" (серия из {item['client_messages']})" if item["client_messages"] > 1 else ""
        )
        detail = (
            f"{fmt_when(item['opened_at'], tz, now)} · «{esc(quote)}»{series} · {why}"
        )
    else:
        reacted = (
            "реакция была" if item["first_reaction_at"] is not None else "полная тишина"
        )
        detail = (
            f"обращение {fmt_when(item['opened_at'], tz, now)} · "
            f"сообщений клиента: {item['client_messages']} · {reacted}"
        )
    return f"{n}. <b>{title}</b>\n    {detail}"


@router.callback_query(DrillAction.filter())
async def on_drill(query: CallbackQuery, callback_data: DrillAction, bot_user: BotUser) -> None:
    """Список обращений критического среза. Строка → выписка переписки."""
    kind = callback_data.kind
    period = callback_data.period or ""
    chat_id = callback_data.chat_id
    page = max(0, callback_data.page)
    # Срез из «Требует внимания» помнит происхождение сентинелом в chat_id:
    # данные те же, что у сводного, но возврат ведёт на экран внимания.
    from_attention = chat_id == DRILL_FROM_ATTENTION
    if from_attention:
        chat_id = 0

    # Право — как у отчёта, из которого провалились: callback data — не доверенный вход.
    needed = SCOPE_PERMS["chat"] if chat_id else SCOPE_PERMS["all"]
    if not has_perm(bot_user, needed):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    if kind not in KIND_LABELS:
        await query.answer("Неизвестный срез", show_alert=True)
        return
    try:
        start, end = period_bounds(period)
    except ValueError:
        await query.answer("Неизвестный период", show_alert=True)
        return

    async with session_scope() as session:
        items, total = await drill_page(session, kind, start, end, chat_id, page)
        calendar_cfg = await get_section(session, "work_calendar")

    tz = calendar_tz(calendar_cfg)
    now = datetime.now(timezone.utc)

    lines = [
        f"<b>{KIND_LABELS[kind]}: {total}</b>",
        f"Период: {period_label(period)}",
        "",
        f"<i>{_DRILL_HINTS[kind]}</i>",
        "",
    ]
    if not items:
        lines.append("Пусто — на этой странице ничего нет.")
    for offset, item in enumerate(items):
        lines.append(_drill_row(item, page * DRILL_PER_PAGE + offset + 1, tz, now, calendar_cfg, kind))
    if total:
        lines.append("")
        lines.append("<i>Кнопка с номером — выписка переписки вокруг обращения.</i>")

    builder = InlineKeyboardBuilder()
    sizes: list[int] = []
    # Строка ведёт в выписку (обработчики alerts_ui). В срезах нарушений — на
    # экран с кнопкой «снять нарушение» (ctxd), в том числе в живых
    # срезах; кнопка живёт внутри нарушения, а не в списке.
    for offset, item in enumerate(items):
        builder.button(
            text=f"💬 {page * DRILL_PER_PAGE + offset + 1}. {(item['title'] or '?')[:28]}",
            callback_data=AlertAction(
                action="ctxd",
                chat_id=item["chat_id"],
                msg_id=item["opened_by_message_id"],
            ).pack(),
        )
        sizes.append(1)

    pages = max(1, -(-total // DRILL_PER_PAGE))
    if pages > 1:
        # На кнопках листания — исходный chat_id с сентинелом: возврат помнит,
        # откуда провалились.
        nav_chat_id = callback_data.chat_id
        nav_row = 0
        if page > 0:
            builder.button(
                text="‹",
                callback_data=DrillAction(
                    kind=kind, period=period, chat_id=nav_chat_id, page=page - 1
                ).pack(),
            )
            nav_row += 1
        if page < pages - 1:
            builder.button(
                text="›",
                callback_data=DrillAction(
                    kind=kind, period=period, chat_id=nav_chat_id, page=page + 1
                ).pack(),
            )
            nav_row += 1
        sizes.append(nav_row)

    if from_attention:
        builder.button(
            text="‹ К «Требует внимания»",
            callback_data=LabAction(kind="attention").pack(),
        )
    else:
        builder.button(
            text="‹ К отчёту",
            callback_data=ReportAction(
                action="run",
                scope="chat" if chat_id else "all",
                period=period,
                target_id=chat_id,
            ).pack(),
        )
    sizes.append(1)
    builder.adjust(*sizes)

    await query.message.edit_text(
        "\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await query.answer()


@router.callback_query(ReportAction.filter(F.action == "run"))
async def on_run(query: CallbackQuery, callback_data: ReportAction, bot_user: BotUser) -> None:
    scope = callback_data.scope or ""
    period = callback_data.period or ""
    target_id = callback_data.target_id

    if not has_perm(bot_user, SCOPE_PERMS.get(scope, "")):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    try:
        start, end = period_bounds(period)
    except ValueError:
        await query.answer("Неизвестный период", show_alert=True)
        return

    body = await _build_report(bot_user, scope, target_id, start, end)
    drill = await _drill_counts_for(scope, target_id, start, end)

    can_export = has_perm(bot_user, Perm.REPORT_EXPORT) and scope != "self"
    text = (
        f"<b>{SCOPE_LABELS.get(scope, scope)}</b>\n"
        f"Период: {period_label(period)}\n\n"
        f"{body}"
    )
    await query.message.edit_text(
        text,
        reply_markup=_result_kb(scope, period, target_id, can_export, drill),
        parse_mode="HTML",
    )
    await query.answer()


# ── Произвольный период: ввод диапазона дат текстом ───────────

_DATE_TOKEN = re.compile(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?")


def parse_period_input(raw: str, today: date) -> tuple[date, date] | None:
    """Разобрать «01.08–15.08», «01.08.2026 - 15.08.2026» или одну дату.

    Год можно опустить — берётся текущий (рабочего пояса). None — не разобрали.
    """
    tokens = _DATE_TOKEN.findall(raw)
    # Мусор между датами допустим (тире, «по»), но больше двух дат — переспросить.
    if not tokens or len(tokens) > 2:
        return None

    parsed: list[date] = []
    for day_s, month_s, year_s in tokens:
        year = int(year_s) if year_s else today.year
        if year < 100:
            year += 2000
        try:
            parsed.append(date(year, int(month_s), int(day_s)))
        except ValueError:
            return None

    first = parsed[0]
    last = parsed[-1]
    if first > last:
        first, last = last, first
    return first, last


@router.callback_query(ReportAction.filter(F.action == "custom"))
async def on_custom(
    query: CallbackQuery, callback_data: ReportAction, state: FSMContext, bot_user: BotUser
) -> None:
    scope = callback_data.scope or ""
    if not has_perm(bot_user, SCOPE_PERMS.get(scope, "")):
        await query.answer("Недостаточно прав", show_alert=True)
        return

    await state.set_state(ReportPeriodForm.waiting_range)
    await state.update_data(scope=scope, target_id=callback_data.target_id)
    await query.message.edit_text(
        f"<b>{SCOPE_LABELS.get(scope, scope)}</b>\n\n"
        "Отправьте период сообщением:\n"
        "• диапазон — <code>01.08–15.08</code>\n"
        "• один день — <code>05.08</code>\n\n"
        "Год можно не писать — возьмётся текущий. С годом тоже можно: "
        "<code>01.08.2026 – 15.08.2026</code>. Обе даты включительно.",
        reply_markup=back_to("reports"),
        parse_mode="HTML",
    )
    await query.answer()


@router.message(ReportPeriodForm.waiting_range)
async def on_custom_range(message: TgMessage, state: FSMContext, bot_user: BotUser) -> None:
    data = await state.get_data()
    scope = data.get("scope", "")
    target_id = data.get("target_id", 0)

    if not has_perm(bot_user, SCOPE_PERMS.get(scope, "")):
        await state.clear()
        await message.answer("Недостаточно прав")
        return

    tz = ZoneInfo(get_settings().tz)
    parsed = parse_period_input(message.text or "", datetime.now(tz).date())
    if parsed is None:
        await message.answer(
            "❌ Не понял даты. Нужно <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>, "
            "диапазон — через тире: <code>01.08–15.08</code>. Попробуйте ещё раз "
            "или вернитесь кнопкой «Назад».",
            parse_mode="HTML",
        )
        return

    first, last = parsed
    period = f"c{first:%Y%m%d}-{last:%Y%m%d}"
    await state.clear()

    start, end = period_bounds(period)
    body = await _build_report(bot_user, scope, target_id, start, end)
    drill = await _drill_counts_for(scope, target_id, start, end)

    can_export = has_perm(bot_user, Perm.REPORT_EXPORT) and scope != "self"
    text = (
        f"<b>{SCOPE_LABELS.get(scope, scope)}</b>\n"
        f"Период: {period_label(period)}\n\n"
        f"{body}"
    )
    await message.answer(
        text,
        reply_markup=_result_kb(scope, period, target_id, can_export, drill),
        parse_mode="HTML",
    )


# ── Выгрузка ──────────────────────────────────────────────────


@router.callback_query(ReportAction.filter(F.action.in_({"expx", "expc"})))
async def on_export(query: CallbackQuery, callback_data: ReportAction, bot_user: BotUser) -> None:
    scope = callback_data.scope or ""
    period = callback_data.period or ""

    # Выгрузка — отдельное право: manager её не имеет.
    if not has_perm(bot_user, Perm.REPORT_EXPORT) or not has_perm(
        bot_user, SCOPE_PERMS.get(scope, "")
    ):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    try:
        start, end = period_bounds(period)
    except ValueError:
        await query.answer("Неизвестный период", show_alert=True)
        return

    await query.answer("Готовлю файл…")
    fmt = "xlsx" if callback_data.action == "expx" else "csv"

    async with session_scope() as session:
        result = await build_export(
            session,
            scope=scope,
            target_id=callback_data.target_id,
            start=start,
            end=end,
            period_label=period_label(period),
            fmt=fmt,
        )

    if result is None:
        await query.message.answer("Не удалось собрать файл: данные не найдены.")
        return

    path, caption, filename = result
    try:
        await query.message.answer_document(
            FSInputFile(path, filename=filename), caption=caption
        )
    finally:
        path.unlink(missing_ok=True)


@router.callback_query(ReportAction.filter(F.action == "exph"))
async def on_export_html(
    query: CallbackQuery, callback_data: ReportAction, bot_user: BotUser
) -> None:
    """HTML-страница сводного отчёта — тот же файл, что в рассылках.
    Право — как у XLSX/CSV."""
    period = callback_data.period or ""
    if callback_data.scope != "all" or not has_perm(bot_user, Perm.REPORT_EXPORT) or not has_perm(
        bot_user, SCOPE_PERMS["all"]
    ):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    try:
        start, end = period_bounds(period)
    except ValueError:
        await query.answer("Неизвестный период", show_alert=True)
        return

    await query.answer("Собираю страницу…")
    from app.services.html_report import write_dashboard

    now = datetime.now(timezone.utc)
    async with session_scope() as session:
        path = await write_dashboard(
            session,
            start=start,
            end=end,
            period_label=period_label(period),
            calendar_cfg=await get_section(session, "work_calendar"),
            now=now,
        )
    try:
        await query.message.answer_document(
            FSInputFile(path, filename=f"dashboard_{now:%Y%m%d_%H%M%S}.html"),
            caption=(
                f"📄 Отчёт страницей за период: {period_label(period)}\n\n"
                "Откройте файл в браузере. Работает без интернета."
            ),
        )
    finally:
        path.unlink(missing_ok=True)


# ── «Снятые нарушения» — журнал решений «снять нарушение» ────

# Свои периоды: журнал считается по моменту решения и включает сегодняшний день,
# а отчётные «последние 7 дней» кончаются вчера.
DISMISSED_PERIODS = {"d7": "последние 7 дней", "d30": "последние 30 дней"}
DISMISSED_DEFAULT = "d7"

# Состояние обращения на СЕЙЧАС — приписка к строке журнала: снятое живое
# обращение могли потом и ответить, и это важно видеть, проверяя решение.
_DISMISSED_STATE_LABELS = {
    "OPEN": "ответа так и нет",
    "REACTED": "откликнулись, ответа по существу нет",
    "ANSWERED": "ответили",
    "NO_RESPONSE_NEEDED": "ответа не требовалось",
    "ABANDONED": "остались без ответа",
}


def dismissed_bounds(period: str) -> tuple[datetime, datetime]:
    """Границы журнала в UTC. d7/d30 — по сегодняшний день включительно."""
    if period in DISMISSED_PERIODS:
        tz = ZoneInfo(get_settings().tz)
        now = datetime.now(tz)
        days = 7 if period == "d7" else 30
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=days - 1
        )
        return start.astimezone(timezone.utc), now.astimezone(timezone.utc)
    # Произвольный период — та же строка «cГГГГММДД-ГГГГММДД», что в отчётах.
    return period_bounds(period)


def dismissed_label(period: str) -> str:
    return DISMISSED_PERIODS.get(period) or period_label(period)


def _dismissed_row(item: dict, n: int, tz, now: datetime) -> str:
    title = _cut(item["title"], 38)
    quote = (item["opener_text"] or "").strip().replace("\n", " ")[:60]
    opened = (
        fmt_when(item["opened_at"], tz, now)
        if item["opened_at"]
        else "время обращения не найдено"
    )
    who = esc(item["who"]) if item["who"] else "неизвестно кем"
    state = item["state"]
    # Обращение могло не пережить пересборку эпизодов — решение остаётся в силе.
    now_label = (
        _DISMISSED_STATE_LABELS.get(getattr(state, "name", ""), "")
        if state is not None
        else "обращение пересобрано"
    )
    lines = [
        f"<b>{n}. {title}</b>",
        f"    обращение {opened}" + (f" · «{esc(quote)}»" if quote else ""),
        f"    снял(а) {who} — {fmt_when(item['dismissed_at'], tz, now)}",
    ]
    if now_label:
        lines.append(f"    сейчас: {now_label}")
    return "\n".join(lines)


def _dismissed_kb(items: list[dict], period: str, page: int, total: int):
    builder = InlineKeyboardBuilder()
    sizes: list[int] = []
    # Кнопка строки — та же выписка, что у алертов, с «↩︎ Вернуть нарушение».
    for offset, item in enumerate(items):
        number = page * DISMISSED_PER_PAGE + offset + 1
        builder.button(
            text=f"💬 {number}. {(item['title'] or '?')[:28]}",
            callback_data=AlertAction(
                action="ctxd",
                chat_id=item["chat_id"],
                msg_id=item["opened_by_message_id"],
            ).pack(),
        )
        sizes.append(1)

    pages = max(1, -(-total // DISMISSED_PER_PAGE))
    if pages > 1:
        nav_row = 0
        if page > 0:
            builder.button(
                text="‹",
                callback_data=DismissedAction(period=period, page=page - 1).pack(),
            )
            nav_row += 1
        if page < pages - 1:
            builder.button(
                text="›",
                callback_data=DismissedAction(period=period, page=page + 1).pack(),
            )
            nav_row += 1
        sizes.append(nav_row)

    for key, label in DISMISSED_PERIODS.items():
        mark = "• " if key == period else ""
        builder.button(
            text=f"{mark}{label.capitalize()}",
            callback_data=DismissedAction(period=key).pack(),
        )
    sizes.append(2)
    builder.button(
        text="📅 Произвольный период",
        callback_data=DismissedAction(action="custom").pack(),
    )
    builder.button(text="‹ К отчётам", callback_data=Nav(to="reports").pack())
    sizes.extend([1, 1])
    builder.adjust(*sizes)
    return builder.as_markup()


async def _dismissed_screen(period: str, page: int) -> tuple[str, Any]:
    start, end = dismissed_bounds(period)
    async with session_scope() as session:
        items, total = await dismissed_page(
            session, start, end, page=page, per_page=DISMISSED_PER_PAGE
        )
        calendar_cfg = await get_section(session, "work_calendar")

    tz = calendar_tz(calendar_cfg)
    now = datetime.now(timezone.utc)
    lines = [
        f"<b>✋ Снятые нарушения: {total}</b>",
        f"Период: {dismissed_label(period)} (по дате решения)",
        "",
        "<i>Нарушения, снятые кнопкой «✔️ Снять нарушение». Они не идут "
        "ни в один счётчик просрочек и ни в одну очередь ожидания. "
        "Здесь видно, что и кем снято, — и можно вернуть обратно.</i>",
        "",
    ]
    if not items:
        lines.append("Пусто — за этот период решений не было.")
    for offset, item in enumerate(items):
        lines.append(
            _dismissed_row(item, page * DISMISSED_PER_PAGE + offset + 1, tz, now)
        )
    if total:
        lines.append("")
        lines.append(
            "<i>Кнопка с номером — переписка вокруг обращения; там же "
            "«↩︎ Вернуть нарушение».</i>"
        )
    return "\n".join(lines), _dismissed_kb(items, period, page, total)


@router.callback_query(DismissedAction.filter(F.action == "custom"))
async def on_dismissed_custom(
    query: CallbackQuery, state: FSMContext, bot_user: BotUser
) -> None:
    if not has_perm(bot_user, Perm.REPORT_ALL_CHATS):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    await state.set_state(DismissedPeriodForm.waiting_range)
    await query.message.edit_text(
        "<b>✋ Снятые нарушения</b>\n\n"
        "Отправьте период сообщением:\n"
        "• диапазон — <code>01.08–15.08</code>\n"
        "• один день — <code>05.08</code>\n\n"
        "Год можно не писать — возьмётся текущий. Обе даты включительно. "
        "Период считается по дате решения.",
        reply_markup=back_to("reports"),
        parse_mode="HTML",
    )
    await query.answer()


@router.message(DismissedPeriodForm.waiting_range)
async def on_dismissed_range(
    message: TgMessage, state: FSMContext, bot_user: BotUser
) -> None:
    if not has_perm(bot_user, Perm.REPORT_ALL_CHATS):
        await state.clear()
        await message.answer("Недостаточно прав")
        return
    tz = ZoneInfo(get_settings().tz)
    parsed = parse_period_input(message.text or "", datetime.now(tz).date())
    if parsed is None:
        await message.answer(
            "❌ Не понял даты. Нужно <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>, "
            "диапазон — через тире: <code>01.08–15.08</code>. Попробуйте ещё раз "
            "или вернитесь кнопкой «Назад».",
            parse_mode="HTML",
        )
        return
    first, last = parsed
    await state.clear()
    text, markup = await _dismissed_screen(f"c{first:%Y%m%d}-{last:%Y%m%d}", 0)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(DismissedAction.filter())
async def on_dismissed(
    query: CallbackQuery, callback_data: DismissedAction, bot_user: BotUser
) -> None:
    """Журнал снятий: что и кем снято за период."""
    # Право то же, что для самого снятия.
    if not has_perm(bot_user, Perm.REPORT_ALL_CHATS):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    period = callback_data.period or DISMISSED_DEFAULT
    try:
        dismissed_bounds(period)
    except ValueError:
        await query.answer("Неизвестный период", show_alert=True)
        return

    await query.answer("Смотрю журнал…")
    text, markup = await _dismissed_screen(period, max(0, callback_data.page))
    try:
        await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        # Повторное нажатие того же периода: Telegram отвергает пустую правку.
        pass
