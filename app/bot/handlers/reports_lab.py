"""Оперативные отчёты: требует внимания, скорость, когда пишут, вне графика,
сводка по алертам.

Открыты по праву на сводный отчёт; только чтение.
"""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.callbacks import (
    DRILL_FROM_ATTENTION,
    AlertAction,
    DrillAction,
    LabAction,
    Nav,
)
from app.bot.handlers.reports import period_bounds, period_label
from app.db.base import session_scope
from app.db.models import BotUser
from app.services.access import Perm, has_perm
from app.services.alert_digest import (
    MANUAL_KIND,
    MANUAL_SLOT,
    DigestView,
    build_alert_digest,
    digest_keyboard,
)
from app.services.report_drill import KIND_NO_ANSWER, drill_counts
from app.services.report_lab import (
    after_hours,
    attention_now,
    load_profile,
    staff_speed,
)
from app.services.settings_store import DEFAULT_REACTION_MINUTES, get_section
from app.services.transcript import (
    WEEKDAYS,
    calendar_tz,
    escape,
    fmt_duration,
    fmt_when,
)

router = Router(name="reports_lab")
# Только приватные диалоги: в группы бот не пишет и меню там не показывает.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

TOP_LIMIT = 10
DEFAULT_PERIOD = "last7"
# Окно для «осталось без ответа» на экране внимания: экран про «сейчас»,
# но снятое с ожидания по определению не «сейчас» — берём последнюю неделю.
NO_ANSWER_PERIOD = "last7"

# Периоды, которые вообще осмысленны для этих отчётов.
LAB_PERIODS = ("today", "last7", "this_month")

# Отчёт → (подпись, нужен ли выбор периода).
REPORTS = {
    "attention": ("🔥 Требует внимания (сейчас)", False),
    "speed": ("⏱ Скорость по сотрудникам", True),
    "load": ("🕐 Когда пишут клиенты", True),
    "night": ("🌙 Работа вне графика", True),
}

# Ручной вызов сводки по алертам «прямо сейчас»; та же кнопка — «Обновить»
# под плановой сводкой.
ALERT_DIGEST_KIND = MANUAL_KIND


@router.callback_query(LabAction.filter(F.kind == ALERT_DIGEST_KIND))
async def on_alert_digest(query: CallbackQuery, bot_user: BotUser) -> None:
    """Та же сводка, что шлёт воркер, но по запросу и на экран."""
    if not has_perm(bot_user, Perm.REPORT_ALL_CHATS):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    async with session_scope() as session:
        view = await build_alert_digest(session, MANUAL_SLOT)
    if view is None:
        view = DigestView(
            text="🧾 Сейчас ни один алерт не горит, а с прошлой сводки ни один "
            "не закрылся — сводка пуста."
        )

    try:
        await query.message.edit_text(
            view.text,
            reply_markup=digest_keyboard(view, back_cb=Nav(to="reports").pack()),
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        # «Обновить» без изменений: Telegram отвергает пустую правку — это не ошибка.
        await query.answer("Ничего не изменилось")
        return
    await query.answer()


def _result_kb(
    kind: str,
    period: str,
    with_periods: bool,
    no_answer: int = 0,
    attention: list[dict] | None = None,
):
    builder = InlineKeyboardBuilder()
    sizes: list[int] = []
    # Из очереди внимания — переход в переписку, тот же обработчик, что в алертах.
    for item in (attention or [])[:6]:
        if not item.get("opened_by_message_id"):
            continue
        # Цвет кнопки совпадает с блоком, где чат показан в тексте: порядок
        # условий тот же, что у корзин в _render_attention.
        if item["reacted"]:
            mark = "🟡"
        elif item["overdue"]:
            mark = "🔴"
        else:
            mark = "🟢"
        builder.button(
            text=f"{mark} {(item['title'] or '?')[:34]}",
            callback_data=AlertAction(
                action="ctxd",
                chat_id=item["chat_id"],
                msg_id=item["opened_by_message_id"],
            ).pack(),
        )
        sizes.append(1)
    if with_periods:
        for key in LAB_PERIODS:
            mark = "• " if key == period else ""
            builder.button(
                text=f"{mark}{period_label(key).capitalize()}",
                callback_data=LabAction(kind=kind, period=key).pack(),
            )
        sizes.append(3)
    if no_answer:
        # «Остались без ответа» не прячутся из очереди внимания.
        builder.button(
            text=f"❓ Остались без ответа: {no_answer}",
            callback_data=DrillAction(
                kind=KIND_NO_ANSWER,
                period=NO_ANSWER_PERIOD,
                chat_id=DRILL_FROM_ATTENTION,
            ).pack(),
        )
        sizes.append(1)
    builder.button(text="🔄 Обновить", callback_data=LabAction(kind=kind, period=period).pack())
    builder.button(text="‹ К отчётам", callback_data=Nav(to="reports").pack())
    sizes.extend([1, 1])
    builder.adjust(*sizes)
    return builder.as_markup()


def _bar(value: int, peak: int, width: int = 10) -> str:
    if peak <= 0:
        return ""
    filled = max(1, round(value / peak * width)) if value else 0
    return "█" * filled + "·" * (width - filled)


@router.callback_query(LabAction.filter(F.kind.in_(set(REPORTS))))
async def on_report(query: CallbackQuery, callback_data: LabAction, bot_user: BotUser) -> None:
    kind = callback_data.kind
    if not has_perm(bot_user, Perm.REPORT_ALL_CHATS):
        await query.answer("Недостаточно прав", show_alert=True)
        return

    label, with_periods = REPORTS[kind]
    period = callback_data.period or DEFAULT_PERIOD
    await query.answer("Считаю…")

    now = datetime.now(timezone.utc)
    start, end = period_bounds(period)
    no_answer = 0
    attention_items: list[dict] = []

    async with session_scope() as session:
        calendar_cfg = await get_section(session, "work_calendar")
        alert_cfg = await get_section(session, "alerts")
        tz = calendar_tz(calendar_cfg)

        if kind == "attention":
            attention_items = await attention_now(
                session,
                calendar_cfg,
                now,
                int(alert_cfg.get("threshold_minutes") or DEFAULT_REACTION_MINUTES),
            )
            body = _render_attention(attention_items, tz, now)
            # Снятое с ожидания — отдельным списком: это уже не «ждёт», но и не решено.
            # Экран про «сейчас»: сегодняшний день включён, хотя «последние 7 дней»
            # в отчётах кончаются вчера.
            na_start, _ = period_bounds(NO_ANSWER_PERIOD)
            na_end = now
            no_answer = (await drill_counts(session, na_start, na_end))[KIND_NO_ANSWER]
            if no_answer:
                body += (
                    f"\n\n❓ За {period_label(NO_ANSWER_PERIOD)} осталось без ответа: "
                    f"<b>{no_answer}</b> — список кнопкой ниже."
                )
        elif kind == "speed":
            body = _render_speed(await staff_speed(session, start, end))
        elif kind == "load":
            body = _render_load(await load_profile(session, start, end, tz.key))
        else:  # night — фильтр обработчика другие kind не пропускает
            body = _render_night(await after_hours(session, start, end, calendar_cfg))

    header = f"<b>{label}</b>"
    if with_periods:
        header += f"\nПериод: {period_label(period)}"
    try:
        await query.message.edit_text(
            f"{header}\n\n{body}",
            reply_markup=_result_kb(
                kind, period, with_periods, no_answer, attention=attention_items
            ),
            parse_mode="HTML",
        )
    except TelegramBadRequest as error:
        # «Обновить» или тот же период без изменений: Telegram отвергает пустую
        # правку — это не ошибка. На нажатие уже ответили «Считаю…».
        if "message is not modified" not in str(error):
            raise


def _render_attention(items: list[dict], tz, now: datetime) -> str:
    """Очередь «за что хвататься», разложенная по видам ожидания."""
    if not items:
        return "✅ Ни одного обращения без ответа. Всё разобрано."

    # Три корзины по срочности и виду ожидания.
    silent_overdue = [i for i in items if i["overdue"] and not i["reacted"]]
    silent_fresh = [i for i in items if not i["overdue"] and not i["reacted"]]
    in_work = [i for i in items if i["reacted"]]

    def block(title: str, rows: list[dict], mark: str, hint: str) -> list[str]:
        if not rows:
            return []
        out = [f"{mark} <b>{title} — {len(rows)}</b>", f"<i>{hint}</i>"]
        for item in rows[:TOP_LIMIT]:
            out.append(
                # Сначала обрезать, потом экранировать: иначе «&amp;» режется пополам.
                f"• <b>{escape((item['title'] or '?')[:38])}</b>\n"
                f"    клиент написал {fmt_when(item['opened_at'], tz, now)}, "
                f"ждёт {fmt_duration(item['business_age'])}"
            )
        if len(rows) > TOP_LIMIT:
            out.append(f"    …и ещё {len(rows) - TOP_LIMIT}")
        out.append("")
        return out

    lines = [f"Ждут ответа: <b>{len(items)}</b>", ""]
    lines += block(
        "Срок вышел, никто не ответил",
        silent_overdue,
        "🔴",
        "Клиенту не ответили вовсе, порог реакции уже нарушен.",
    )
    # Все обращения после первой реакции — и переданные специалисту, и те,
    # где менеджер ответил «принято».
    lines += block(
        "После реакции ждут ответа",
        in_work,
        "🟡",
        "Менеджер откликнулся или передал вопрос специалисту — ответа по существу ещё нет.",
    )
    lines += block(
        "Новые, в пределах срока",
        silent_fresh,
        "🟢",
        "Клиент написал недавно, время на ответ ещё есть.",
    )
    return "\n".join(lines).rstrip()


def _render_speed(rows: list[dict]) -> str:
    if not rows:
        return "За период никто не отвечал на обращения — считать нечего."

    lines = ["<i>Сверху — у кого больше просрочек.</i>", ""]
    for row in rows[:TOP_LIMIT]:
        mark = "⚠️ " if row["breached"] else "✅ "
        lines.append(f"{mark}<b>{escape(row['full_name'])}</b>")
        lines.append(f"    обращений: {row['episodes']}")
        lines.append(
            f"    отвечает обычно за {fmt_duration(row['median'] or 0)}, "
            f"9 из 10 — быстрее, чем {fmt_duration(row['p90'] or 0)}"
        )
        lines.append(
            f"    просрочек: {row['breached']}"
            if row["breached"]
            else "    просрочек нет"
        )
        lines.append("")
    lines.append(
        "<i>⚠️ — есть просрочки, ✅ — просрочек нет.\n"
        "Считается время до первой реакции в рабочих минутах.\n"
        "«Обычно» — половина обращений разобрана быстрее, половина дольше.\n"
        "«9 из 10 — быстрее, чем» — девять обращений из десяти уложились в это "
        "время, десятое ждало дольше.\n"
        "Среднее не берём: один долгий ответ исказил бы всю картину.</i>"
    )
    return "\n".join(lines)


def _render_load(data: dict) -> str:
    hours, weekdays = data["hours"], data["weekdays"]
    if not hours:
        return "За период сообщений от клиентов не было."

    lines = ["<b>По часам</b>"]
    peak = max(hours.values())
    for hour in sorted(hours):
        count = hours[hour]
        lines.append(f"<code>{hour:02d}:00</code> {_bar(count, peak)} {count}")

    lines.append("")
    lines.append("<b>По дням недели</b>")
    peak_day = max(weekdays.values()) if weekdays else 0
    for day in sorted(weekdays):
        count = weekdays[day]
        lines.append(f"<code>{WEEKDAYS[day - 1]}</code>   {_bar(count, peak_day)} {count}")

    lines.append("")
    lines.append("<i>Только сообщения клиентов, время рабочего пояса.</i>")
    return "\n".join(lines)


def _render_night(rows: list[dict]) -> str:
    if not rows:
        return "✅ Вне графика за период никто не писал."

    lines = []
    for row in rows[:TOP_LIMIT]:
        share = round(row["outside"] / row["total"] * 100) if row["total"] else 0
        lines.append(
            f"• <b>{escape(row['full_name'])}</b> — "
            f"{row['outside']} из {row['total']} сообщений ({share}%)"
        )
    lines.append("")
    lines.append(
        "<i>Вне графика — по рабочему календарю: вечера, выходные и праздники. "
        "Показатель для разговора о нагрузке, а не для премии.</i>"
    )
    return "\n".join(lines)


@router.callback_query(LabAction.filter())
async def on_report_gone(query: CallbackQuery, bot_user: BotUser) -> None:
    """Кнопки отчётов, которых нет в текущей версии. Регистрируется после
    остальных обработчиков LabAction и ловит только то, что они не взяли, —
    иначе старая кнопка крутила бы спиннер."""
    await query.answer(
        "Этого отчёта больше нет. Откройте «Отчёты» заново — состав обновился.",
        show_alert=True,
    )
