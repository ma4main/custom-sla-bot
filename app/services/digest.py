"""Плановые рассылки: вечерняя сводка дня, недельный и месячный отчёты.

⚠️ В сводке просрочка приписывается тому, кто ответил (пусть и поздно);
обращения, где не ответил никто, видны отдельной строкой «осталось без ответа».
"""

from __future__ import annotations

from datetime import datetime, time as _time, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import effective_notify_group_id
from app.db.models import Attribution, Message, Setting, Staff
from app.services.calendar import _parse_hhmm, is_workday  # noqa: PLC2701
from app.services.report_drill import KIND_NO_ANSWER, drill_page
from app.services.report_lab import problem_chats, staff_speed
from app.services.settings_store import get_section
from app.services.tracking import observed_filter
from app.services.transcript import calendar_tz, escape, fmt_duration

log = structlog.get_logger(__name__)

# Ключ строки состояния в setting: память рассылки (какие выпуски уже ушли),
# а не настройка человека, поэтому не в DEFAULTS.
STATE_KEY = "digest_runtime"

STAFF_LIMIT = 15
CHAT_LIMIT = 8


def should_send(
    now: datetime,
    digest_cfg: dict[str, Any],
    calendar_cfg: dict[str, Any],
    last_sent_date: str | None,
) -> bool:
    """Пора ли слать сводку. «Раз в день» держится датой последней отправки,
    а не таймером, чтобы рестарт воркера не задваивал и не терял сводку."""
    if not digest_cfg.get("evening_enabled"):
        return False
    if not is_workday(now, calendar_cfg):
        return False

    tz = calendar_tz(calendar_cfg)
    local = now.astimezone(tz)
    if last_sent_date == local.date().isoformat():
        return False

    send_at = _parse_hhmm(str(digest_cfg.get("evening_time") or "19:05"), _time(19, 5))
    return local.time() >= send_at


async def _staff_rows(
    session: AsyncSession, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    speed = {row["staff_id"]: row for row in await staff_speed(session, start, end)}

    volume_rows = (
        await session.execute(
            select(
                Staff.id,
                func.count(Message.id).label("messages"),
                func.count(func.distinct(Message.chat_id)).label("chats_touched"),
            )
            .join(Attribution, Attribution.staff_id == Staff.id)
            .join(Message, Message.id == Attribution.message_id)
            .where(observed_filter())
            .where(Message.sent_at >= start, Message.sent_at < end)
            .group_by(Staff.id)
        )
    ).all()
    volume = {row.id: row for row in volume_rows}

    people = (
        await session.scalars(
            select(Staff).where(Staff.active.is_(True)).order_by(Staff.full_name)
        )
    ).all()

    rows = []
    for person in people:
        fast = speed.get(person.id)
        vol = volume.get(person.id)
        rows.append(
            {
                "full_name": person.full_name,
                "messages": vol.messages if vol else 0,
                "chats_touched": vol.chats_touched if vol else 0,
                "episodes": fast["episodes"] if fast else 0,
                "median": fast["median"] if fast else None,
                "breached": fast["breached"] if fast else 0,
            }
        )
    rows.sort(key=lambda r: (-r["breached"], -r["messages"], r["full_name"]))
    return rows


async def build_digest(
    session: AsyncSession, calendar_cfg: dict[str, Any], now: datetime
) -> str:
    """Текст сводки за сегодня (с местной полуночи до сейчас)."""
    tz = calendar_tz(calendar_cfg)
    local = now.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    staff = await _staff_rows(session, start, now)
    no_answer_items, no_answer_total = await drill_page(
        session, KIND_NO_ANSWER, start, now, per_page=3
    )
    chats = [row for row in await problem_chats(session, start, now) if row["breached"]]

    lines = [f"📊 <b>Сводка за {local.strftime('%d.%m')}</b>", ""]

    if staff:
        lines.append("<b>По сотрудникам</b>")
        for row in staff[:STAFF_LIMIT]:
            if row["messages"] == 0 and row["episodes"] == 0:
                lines.append(f"• {escape(row['full_name'])} — активности не было")
                continue
            parts = []
            if row["breached"]:
                parts.append(f"нарушений: {row['breached']}")
            parts.append(f"чатов: {row['chats_touched']}")
            parts.append(f"сообщений: {row['messages']}")
            if row["median"] is not None:
                parts.append(f"медиана {fmt_duration(row['median'])}")
            mark = "⚠️ " if row["breached"] else ""
            lines.append(f"• {mark}{escape(row['full_name'])} — {', '.join(parts)}")
        if len(staff) > STAFF_LIMIT:
            lines.append(f"…и ещё {len(staff) - STAFF_LIMIT}")
        lines.append("")

    if no_answer_total:
        names = ", ".join(escape((item["title"] or "?")[:30]) for item in no_answer_items)
        more = "…" if no_answer_total > len(no_answer_items) else ""
        lines.append(f"❓ <b>Осталось без ответа: {no_answer_total}</b> ({names}{more})")
        lines.append("")

    if chats:
        lines.append("<b>Нарушения по чатам</b>")
        for row in chats[:CHAT_LIMIT]:
            lines.append(f"• {escape((row['title'] or '?')[:38])} — {row['breached']}")
    else:
        lines.append("✅ Ни одной просрочки за день.")
    lines.append("")

    lines.append(
        "<i>Нарушение здесь — первая реакция позже порога; срок специалиста "
        "после передачи считается отдельно, в сводках по алертам и в отчёте "
        "«Скорость ответа». Просрочка записана тому, "
        "кто в итоге ответил; где не ответил никто — строка «осталось без "
        "ответа», такие никому не приписаны.</i>"
    )
    return "\n".join(lines)


async def _load_state(session: AsyncSession, field: str = "last_sent") -> str | None:
    stored = await session.get(Setting, STATE_KEY)
    if stored is not None and isinstance(stored.value, dict):
        return stored.value.get(field)
    return None


async def _save_state(session: AsyncSession, value: str, field: str = "last_sent") -> None:
    stored = await session.get(Setting, STATE_KEY)
    if stored is None:
        session.add(Setting(key=STATE_KEY, value={field: value}))
    else:
        stored.value = {**(stored.value or {}), field: value}


async def _digest_targets(session: AsyncSession, digest_cfg: dict[str, Any]) -> list[int]:
    """Лички по праву алертов (и флагу «в личку») плюс группа, если включена."""
    from app.services.access import personal_alert_recipients

    targets: list[int] = [
        user.tg_user_id for user in await personal_alert_recipients(session)
    ]
    notify_group_id = effective_notify_group_id()
    if bool(digest_cfg.get("to_group")) and notify_group_id is not None:
        targets.append(notify_group_id)
    return targets


async def claim_evening_digest(session: AsyncSession) -> bool:
    """Забронировать сегодняшнюю сводку: проверка «пора ли» плюс отметка.

    Бронь коммитится отдельной транзакцией до вызова Telegram: сбой сети
    после брони не задваивает рассылку.
    """
    digest_cfg = await get_section(session, "digest")
    calendar_cfg = await get_section(session, "work_calendar")
    now = datetime.now(timezone.utc)

    if not should_send(now, digest_cfg, calendar_cfg, await _load_state(session)):
        return False

    local_date = now.astimezone(calendar_tz(calendar_cfg)).date().isoformat()
    await _save_state(session, local_date)
    return True


async def evening_payload(session: AsyncSession) -> tuple[str, list[int]] | None:
    """Текст сводки и адресаты. None — слать некому."""
    digest_cfg = await get_section(session, "digest")
    calendar_cfg = await get_section(session, "work_calendar")
    now = datetime.now(timezone.utc)

    targets = await _digest_targets(session, digest_cfg)
    if not targets:
        log.warning("digest.no_recipients")
        return None
    return await build_digest(session, calendar_cfg, now), targets


async def maybe_send_evening_digest(bot) -> bool:
    """Вызывается каждым тиком воркера; сам решает, пора ли.

    Бронь и сборка — короткими транзакциями, сеть — вне транзакции.
    """
    from app.db.base import session_scope

    async with session_scope() as session:
        if not await claim_evening_digest(session):
            return False

    try:
        async with session_scope() as session:
            payload = await evening_payload(session)
    except Exception:  # noqa: BLE001 — выпуск уходит в досылку
        await _remember_build_failure("evening")
        return False
    if payload is None:
        return False

    text, targets = payload
    failed = await _send_issue(bot, "evening", text, [], targets)
    await _remember_failures("evening", failed)
    return len(failed) < len(targets)


# Бронь выпуска коммитится до сети, поэтому недоставленное не теряется иначе:
# адресаты, которым не дошла хотя бы одна часть, запоминаются, и следующие
# тики досылают им выпуск целиком (пересобранный), до RETRY_ATTEMPTS раз.

RETRY_ATTEMPTS = 3


async def _send_issue(
    bot, kind: str, text: str, documents: list[tuple[Any, str, str]], targets: list[int]
) -> list[int]:
    """Отправить текст и документы (путь, имя файла, подпись) каждому адресату.

    Возвращает, кому не дошло; доставленным считается только адресат, получивший все части.
    """
    from aiogram.types import FSInputFile

    failed: list[int] = []
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
            for path, filename, caption in documents:
                await bot.send_document(
                    chat_id, FSInputFile(path, filename=filename), caption=caption
                )
        except Exception:  # noqa: BLE001 — недоставленное не роняет тик
            log.exception("digest.send_failed", kind=kind, chat_id=chat_id)
            failed.append(chat_id)
    log.info(
        "digest.sent", kind=kind, delivered=len(targets) - len(failed), targets=len(targets)
    )
    return failed


async def _remember_failures(
    kind: str, failed: list[int], *, attempts: int | None = None
) -> None:
    """Запомнить недоставленное для досылки. `attempts` — новый счёт попыток;
    без него счёт прежний."""
    from app.db.base import session_scope

    async with session_scope() as session:
        stored = await session.get(Setting, STATE_KEY)
        state = dict(stored.value or {}) if stored is not None else {}
        retry = dict(state.get("retry") or {})
        if failed:
            previous = retry.get(kind) or {}
            retry[kind] = {
                "targets": sorted(set(failed)),
                "attempts": (
                    int(previous.get("attempts") or 0) if attempts is None else attempts
                ),
            }
        else:
            retry.pop(kind, None)
        state["retry"] = retry
        if stored is None:
            session.add(Setting(key=STATE_KEY, value=state))
        else:
            stored.value = state


async def _remember_build_failure(kind: str) -> None:
    """Сборка выпуска упала после брони: бронь не снимается (иначе каждый тик собирал бы
    выпуск заново), а все текущие адресаты уходят в досылку как недоставленные —
    её попытки ограничены RETRY_ATTEMPTS.
    """
    from app.db.base import session_scope

    log.exception("digest.build_failed", kind=kind)
    async with session_scope() as session:
        targets = await _digest_targets(session, await get_section(session, "digest"))
    await _remember_failures(kind, targets)


async def pending_retries(session: AsyncSession) -> dict[str, dict[str, Any]]:
    stored = await session.get(Setting, STATE_KEY)
    if stored is None or not isinstance(stored.value, dict):
        return {}
    return dict(stored.value.get("retry") or {})


async def retry_undelivered_issues(bot) -> int:
    """Дослать выпуски адресатам, которым не дошло. Возвращает число досылок."""
    from app.db.base import session_scope

    async with session_scope() as session:
        pending = await pending_retries(session)
    if not pending:
        return 0

    builders = {
        "evening": evening_payload,
        "weekly": weekly_payload,
        "monthly": monthly_payload,
    }
    resent = 0
    for kind, entry in pending.items():
        builder = builders.get(kind)
        targets = [int(t) for t in entry.get("targets") or []]
        attempts = int(entry.get("attempts") or 0)
        if builder is None or not targets or attempts >= RETRY_ATTEMPTS:
            if attempts >= RETRY_ATTEMPTS:
                log.error("digest.retry_exhausted", kind=kind, targets=targets)
            await _remember_failures(kind, [])
            continue

        try:
            async with session_scope() as session:
                payload = await builder(session)
        except Exception:  # noqa: BLE001 — упавшая сборка — неудачная попытка досылки
            log.exception("digest.build_failed", kind=kind)
            await _remember_failures(kind, targets, attempts=attempts + 1)
            continue
        if payload is None:
            await _remember_failures(kind, [])
            continue

        text, current_targets, *rest = payload
        # Досылаем только тем, кому выпуск положен сейчас: доступ или личку
        # могли выключить после первой попытки.
        allowed = {int(t) for t in (current_targets or [])}
        targets = [t for t in targets if t in allowed]
        documents: list[tuple[Any, str, str]] = []
        cleanup: list[Any] = []
        export = rest[0] if rest else None
        html_path = rest[1] if len(rest) > 1 else None
        if export is not None:
            path, caption, filename = export
            documents.append((path, filename, caption))
            cleanup.append(path)
        if html_path is not None:
            documents.append(
                (html_path, f"отчёт_{'за_неделю' if kind == 'weekly' else 'за_месяц'}.html",
                 "📄 Отчёт страницей — откройте файл в браузере. Работает без интернета.")
            )
            cleanup.append(html_path)
        try:
            failed = await _send_issue(bot, kind, text, documents, targets) if targets else []
        finally:
            for path in cleanup:
                path.unlink(missing_ok=True)

        async with session_scope() as session:
            stored = await session.get(Setting, STATE_KEY)
            state = dict(stored.value or {}) if stored is not None else {}
            retry = dict(state.get("retry") or {})
            if failed:
                retry[kind] = {"targets": sorted(set(failed)), "attempts": attempts + 1}
            else:
                retry.pop(kind, None)
            state["retry"] = retry
            if stored is not None:
                stored.value = state
        resent += len(targets) - len(failed)
        log.warning("digest.retried", kind=kind, resent=len(targets) - len(failed), failed=failed)
    return resent


def _weekly_due_key(local: datetime, day: int) -> str:
    """Ключ дедупликации: ISO-неделя последнего наступления настроенного дня.

    Ключ по дню-цели, а не по моменту отправки: досылка в следующей ISO-неделе
    не должна съедать следующий выпуск.
    """
    due = local.date() - timedelta(days=(local.isoweekday() - day) % 7)
    iso = due.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def should_send_weekly(
    now: datetime,
    digest_cfg: dict[str, Any],
    calendar_cfg: dict[str, Any],
    last_sent_week: str | None,
) -> bool:
    """Пора ли слать недельный отчёт.

    Отсчёт от последнего наступления настроенного дня (1=пн … 7=вс): если он
    выпал на выходной или праздник, отчёт уходит в первый рабочий день после.
    """
    if not digest_cfg.get("weekly_enabled"):
        return False
    if not is_workday(now, calendar_cfg):
        return False

    tz = calendar_tz(calendar_cfg)
    local = now.astimezone(tz)
    day = int(digest_cfg.get("weekly_day") or 1)
    if last_sent_week == _weekly_due_key(local, day):
        return False

    if local.isoweekday() != day:
        # Не день рассылки, но выпуск за последнее его наступление ещё не ушёл —
        # досылаем первым рабочим тиком, время суток не сверяем.
        return True

    send_at = _parse_hhmm(str(digest_cfg.get("weekly_time") or "10:05"), _time(10, 5))
    return local.time() >= send_at


def _previous_week(now: datetime, calendar_cfg: dict[str, Any]) -> tuple[datetime, datetime]:
    tz = calendar_tz(calendar_cfg)
    local = now.astimezone(tz)
    today0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
    monday_this = today0 - timedelta(days=local.isoweekday() - 1)
    start = monday_this - timedelta(days=7)
    return start.astimezone(timezone.utc), monday_this.astimezone(timezone.utc)


async def claim_weekly_report(session: AsyncSession) -> bool:
    """Забронировать недельный выпуск (как у сводки: бронь до сети)."""
    digest_cfg = await get_section(session, "digest")
    calendar_cfg = await get_section(session, "work_calendar")
    now = datetime.now(timezone.utc)

    if not should_send_weekly(
        now, digest_cfg, calendar_cfg, await _load_state(session, "weekly_last_sent")
    ):
        return False

    local = now.astimezone(calendar_tz(calendar_cfg))
    day = int(digest_cfg.get("weekly_day") or 1)
    await _save_state(session, _weekly_due_key(local, day), "weekly_last_sent")
    return True


async def weekly_payload(session: AsyncSession):
    """(текст, адресаты, xlsx | None, html | None) недельного отчёта; None — слать некому.

    HTML собирается только при включённом digest.weekly_html.
    """
    digest_cfg = await get_section(session, "digest")
    calendar_cfg = await get_section(session, "work_calendar")
    now = datetime.now(timezone.utc)

    targets = await _digest_targets(session, digest_cfg)
    if not targets:
        log.warning("digest.weekly_no_recipients")
        return None

    tz = calendar_tz(calendar_cfg)

    # Период выпуска — настройкой; границы last7 те же, что у кнопки в отчётах
    # (period_bounds), чтобы рассылка совпадала с тем, что видно по кнопке.
    if str(digest_cfg.get("weekly_period") or "prev_week") == "last7":
        from app.bot.handlers.reports import period_bounds

        start, end = period_bounds("last7")
        span = (
            f"{start.astimezone(tz).strftime('%d.%m')}–"
            f"{(end - timedelta(seconds=1)).astimezone(tz).strftime('%d.%m')}"
        )
        title = f"📈 <b>Отчёт за последние 7 дней ({span})</b>"
        html_label = "последние 7 дней"
    else:
        start, end = _previous_week(now, calendar_cfg)
        span = (
            f"{start.astimezone(tz).strftime('%d.%m')}–"
            f"{(end.astimezone(tz) - timedelta(days=1)).strftime('%d.%m')}"
        )
        title = f"📈 <b>Отчёт за неделю {span}</b>"
        html_label = "прошлая неделя"

    # Текст — тот же рендер, что «Сводный по всем чатам»; раскладка — из настройки.
    from app.bot.handlers.reports import _render_all

    body = await _render_all(
        session, start, end, layout=str(digest_cfg.get("report_layout") or "full")
    )
    text = f"{title}\n\n{body}"

    export = None
    if bool(digest_cfg.get("weekly_xlsx", True)):
        from app.services.export import build_export

        export = await build_export(
            session,
            scope="all",
            target_id=0,
            start=start,
            end=end,
            period_label=f"неделя {span}",
            fmt="xlsx",
        )

    html_path = None
    if bool(digest_cfg.get("weekly_html")):
        from app.services.html_report import write_dashboard

        # Даты страница печатает сама из start/end — в подписи периода их нет, иначе удвоятся.
        html_path = await write_dashboard(
            session,
            start=start,
            end=end,
            period_label=html_label,
            calendar_cfg=calendar_cfg,
            now=now,
        )
    return text, targets, export, html_path


async def maybe_send_weekly_report(bot) -> bool:
    from app.db.base import session_scope

    async with session_scope() as session:
        if not await claim_weekly_report(session):
            return False

    try:
        async with session_scope() as session:
            payload = await weekly_payload(session)
    except Exception:  # noqa: BLE001 — выпуск уходит в досылку
        await _remember_build_failure("weekly")
        return False
    if payload is None:
        return False

    text, targets, export, html_path = payload
    documents: list[tuple[Any, str, str]] = []
    if export is not None:
        path, caption, filename = export
        documents.append((path, filename, caption))
    if html_path is not None:
        documents.append(
            (
                html_path,
                "отчёт_за_неделю.html",
                "📄 Отчёт страницей — откройте файл в браузере. Работает без интернета.",
            )
        )
    try:
        failed = await _send_issue(bot, "weekly", text, documents, targets)
    finally:
        if export is not None:
            export[0].unlink(missing_ok=True)
        if html_path is not None:
            html_path.unlink(missing_ok=True)
    await _remember_failures("weekly", failed)
    return len(failed) < len(targets)


def _monthly_due_key(local: datetime, day: int) -> str:
    """Ключ дедупликации: месяц последнего наступления настроенного дня (как у недельного)."""
    if local.day >= day:
        return f"{local.year}-{local.month:02d}"
    prev_last = local.replace(day=1) - timedelta(days=1)
    return f"{prev_last.year}-{prev_last.month:02d}"


def should_send_monthly(
    now: datetime,
    digest_cfg: dict[str, Any],
    calendar_cfg: dict[str, Any],
    last_sent_month: str | None,
) -> bool:
    """Пора ли слать месячный отчёт.

    День-цель 1–28; выпал на выходной или праздник — отчёт уходит первым рабочим днём после.
    """
    if not digest_cfg.get("monthly_enabled"):
        return False
    if not is_workday(now, calendar_cfg):
        return False

    tz = calendar_tz(calendar_cfg)
    local = now.astimezone(tz)
    day = int(digest_cfg.get("monthly_day") or 1)
    if last_sent_month == _monthly_due_key(local, day):
        return False

    if local.day != day:
        # Не день рассылки, но выпуск за последнее наступление ещё не ушёл —
        # досылаем первым рабочим тиком, время суток не сверяем.
        return True

    send_at = _parse_hhmm(str(digest_cfg.get("monthly_time") or "10:05"), _time(10, 5))
    return local.time() >= send_at


def _previous_month(now: datetime, calendar_cfg: dict[str, Any]) -> tuple[datetime, datetime]:
    tz = calendar_tz(calendar_cfg)
    local = now.astimezone(tz)
    this_first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_first = (this_first - timedelta(days=1)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return prev_first.astimezone(timezone.utc), this_first.astimezone(timezone.utc)


async def claim_monthly_report(session: AsyncSession) -> bool:
    digest_cfg = await get_section(session, "digest")
    calendar_cfg = await get_section(session, "work_calendar")
    now = datetime.now(timezone.utc)

    if not should_send_monthly(
        now, digest_cfg, calendar_cfg, await _load_state(session, "monthly_last_sent")
    ):
        return False

    local = now.astimezone(calendar_tz(calendar_cfg))
    day = int(digest_cfg.get("monthly_day") or 1)
    await _save_state(session, _monthly_due_key(local, day), "monthly_last_sent")
    return True


async def monthly_payload(session: AsyncSession):
    """(текст, адресаты, xlsx | None, html | None) месячного отчёта; None — слать некому."""
    digest_cfg = await get_section(session, "digest")
    calendar_cfg = await get_section(session, "work_calendar")
    now = datetime.now(timezone.utc)

    targets = await _digest_targets(session, digest_cfg)
    if not targets:
        log.warning("digest.monthly_no_recipients")
        return None

    tz = calendar_tz(calendar_cfg)
    start, end = _previous_month(now, calendar_cfg)
    local_start = start.astimezone(tz)
    span = f"{local_start.strftime('%d.%m')}–{(end.astimezone(tz) - timedelta(days=1)).strftime('%d.%m.%Y')}"

    from app.bot.handlers.reports import _render_all

    body = await _render_all(
        session, start, end, layout=str(digest_cfg.get("report_layout") or "full")
    )
    text = f"📊 <b>Отчёт за месяц {span}</b>\n\n{body}"

    export = None
    if bool(digest_cfg.get("monthly_xlsx", True)):
        from app.services.export import build_export

        export = await build_export(
            session,
            scope="all",
            target_id=0,
            start=start,
            end=end,
            period_label=f"месяц {span}",
            fmt="xlsx",
        )

    html_path = None
    if bool(digest_cfg.get("monthly_html")):
        from app.services.html_report import write_dashboard

        html_path = await write_dashboard(
            session,
            start=start,
            end=end,
            period_label="прошлый месяц",
            calendar_cfg=calendar_cfg,
            now=now,
        )
    return text, targets, export, html_path


async def maybe_send_monthly_report(bot) -> bool:
    from app.db.base import session_scope

    async with session_scope() as session:
        if not await claim_monthly_report(session):
            return False

    try:
        async with session_scope() as session:
            payload = await monthly_payload(session)
    except Exception:  # noqa: BLE001 — выпуск уходит в досылку
        await _remember_build_failure("monthly")
        return False
    if payload is None:
        return False

    text, targets, export, html_path = payload
    documents: list[tuple[Any, str, str]] = []
    if export is not None:
        path, caption, filename = export
        documents.append((path, filename, caption))
    if html_path is not None:
        documents.append(
            (
                html_path,
                "отчёт_за_месяц.html",
                "📄 Отчёт страницей — откройте файл в браузере. Работает без интернета.",
            )
        )
    try:
        failed = await _send_issue(bot, "monthly", text, documents, targets)
    finally:
        if export is not None:
            export[0].unlink(missing_ok=True)
        if html_path is not None:
            html_path.unlink(missing_ok=True)
    await _remember_failures("monthly", failed)
    return len(failed) < len(targets)


async def suppress_pending_issue(
    session: AsyncSession, *, weekly: bool = False, monthly: bool = False
) -> None:
    """Пометить текущий выпуск отправленным — без отправки.

    Вызывается при включении рассылки и смене её дня: иначе правило досылки
    сочло бы пустую отметку пропущенным выпуском и отправило отчёт сразу.
    """
    digest_cfg = await get_section(session, "digest")
    calendar_cfg = await get_section(session, "work_calendar")
    local = datetime.now(timezone.utc).astimezone(calendar_tz(calendar_cfg))

    if weekly:
        day = int(digest_cfg.get("weekly_day") or 1)
        await _save_state(session, _weekly_due_key(local, day), "weekly_last_sent")
        log.info("digest.weekly_backlog_suppressed", day=day)
    if monthly:
        day = int(digest_cfg.get("monthly_day") or 1)
        await _save_state(session, _monthly_due_key(local, day), "monthly_last_sent")
        log.info("digest.monthly_backlog_suppressed", day=day)
