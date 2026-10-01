"""Рабочий календарь: рабочее время между моментами и срок ответа.

  business_seconds   — сколько рабочего времени прошло (метрика скорости);
  response_deadline  — к какому моменту ответ обязан прозвучать (срок).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.services.settings_store import DEFAULT_WORKDAY_END, DEFAULT_WORKDAY_START
from app.services.versioning import History

# Предел поиска рабочего дня: длинные праздники бывают, бесконечный цикл — нет.
_MAX_LOOKAHEAD_DAYS = 400


def _parse_hhmm(value: str, fallback: time) -> time:
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError):
        return fallback


_DEFAULT_START = time.fromisoformat(DEFAULT_WORKDAY_START)
_DEFAULT_END = time.fromisoformat(DEFAULT_WORKDAY_END)


def _params(config: dict[str, Any]):
    tz = ZoneInfo(config.get("timezone") or "Europe/Moscow")
    weekdays = set(config.get("weekdays") or [1, 2, 3, 4, 5])
    day_start = _parse_hhmm(config.get("start") or DEFAULT_WORKDAY_START, _DEFAULT_START)
    day_end = _parse_hhmm(config.get("end") or DEFAULT_WORKDAY_END, _DEFAULT_END)
    holidays = set(config.get("holidays") or [])
    return tz, weekdays, day_start, day_end, holidays


def _is_workday(day: date, weekdays: set[int], holidays: set[str]) -> bool:
    return (day.isoweekday() in weekdays) and (day.isoformat() not in holidays)


def is_workday(moment: datetime, config: dict[str, Any]) -> bool:
    """Рабочий ли календарный день у момента (в рабочем поясе). Про день, а не часы:
    вечер рабочего дня — всё ещё рабочий день.
    """
    tz, weekdays, _, _, holidays = _params(config)
    return _is_workday(moment.astimezone(tz).date(), weekdays, holidays)


def business_seconds(start: datetime, end: datetime, config: dict[str, Any]) -> int:
    """Рабочие секунды между start и end (aware, любой пояс). Конфиг: weekdays
    (1 = понедельник … 7 = воскресенье), start/end "HH:MM", timezone, holidays ["YYYY-MM-DD", …].
    """
    if end <= start:
        return 0

    tz, weekdays, day_start, day_end, holidays = _params(config)

    start_local = start.astimezone(tz)
    end_local = end.astimezone(tz)

    total = 0
    cursor = start_local.date()
    while cursor <= end_local.date():
        if _is_workday(cursor, weekdays, holidays):
            window_open = datetime.combine(cursor, day_start, tzinfo=tz)
            window_close = datetime.combine(cursor, day_end, tzinfo=tz)
            lo = max(start_local, window_open)
            hi = min(end_local, window_close)
            if hi > lo:
                total += int((hi - lo).total_seconds())
        cursor += timedelta(days=1)

    return total


def same_time_next_workday(
    start: datetime, seconds: int, config: dict[str, Any]
) -> datetime | None:
    """Срок для долгих порогов: «то же время следующего рабочего дня».

    Календарный порог прибавляется; нерабочий день переносится на ближайший рабочий
    с тем же временем суток, нерабочие часы прижимаются к границе окна. Не `response_deadline`:
    тот умещает порог внутри одного рабочего дня. None — рабочего дня нет на год вперёд.
    """
    if seconds <= 0:
        return start

    tz, weekdays, day_start, day_end, holidays = _params(config)
    target = (start + timedelta(seconds=seconds)).astimezone(tz)

    cursor = target.date()
    for _ in range(_MAX_LOOKAHEAD_DAYS):
        if _is_workday(cursor, weekdays, holidays):
            window_open = datetime.combine(cursor, day_start, tzinfo=tz)
            window_close = datetime.combine(cursor, day_end, tzinfo=tz)
            if window_close > window_open:
                moment = datetime.combine(cursor, target.time(), tzinfo=tz)
                moment = min(max(moment, window_open), window_close)
                return moment.astimezone(timezone.utc)
        cursor += timedelta(days=1)

    return None


def next_working_moment(
    moment: datetime, config: dict[str, Any]
) -> datetime | None:
    """Ближайший рабочий момент (сам `moment`, если он рабочий). Конец рабочего дня
    рабочим не считается. None — рабочего дня нет на год вперёд.
    """
    tz, weekdays, day_start, day_end, holidays = _params(config)
    local = moment.astimezone(tz)

    cursor = local.date()
    for _ in range(_MAX_LOOKAHEAD_DAYS):
        if _is_workday(cursor, weekdays, holidays):
            window_open = datetime.combine(cursor, day_start, tzinfo=tz)
            window_close = datetime.combine(cursor, day_end, tzinfo=tz)
            if window_close > window_open:
                if local < window_open:
                    return window_open.astimezone(timezone.utc)
                if local < window_close:
                    return moment
        cursor += timedelta(days=1)

    return None


def response_deadline(
    start: datetime, seconds: int, config: dict[str, Any]
) -> datetime | None:
    """Момент, к которому ответ обязан прозвучать.

    Порог НЕ переносится через нерабочее время по частям: новый рабочий день даёт его
    целиком заново, отсчёт — с открытия. None — рабочего дня нет на год вперёд: срок
    не наступает, алерты не шлются. Пустой weekdays откатывается к будням.
    """
    if seconds <= 0:
        return start

    tz, weekdays, day_start, day_end, holidays = _params(config)
    start_local = start.astimezone(tz)

    first_open: datetime | None = None
    cursor = start_local.date()
    for _ in range(_MAX_LOOKAHEAD_DAYS):
        if _is_workday(cursor, weekdays, holidays):
            window_open = datetime.combine(cursor, day_start, tzinfo=tz)
            window_close = datetime.combine(cursor, day_end, tzinfo=tz)
            if window_close > window_open:
                if first_open is None:
                    first_open = max(start_local, window_open)
                # Внутри дня таймер идёт с обращения, в следующие дни — с открытия.
                entry = max(start_local, window_open)
                if entry < window_close:
                    if (window_close - entry).total_seconds() >= seconds:
                        return (entry + timedelta(seconds=seconds)).astimezone(
                            timezone.utc
                        )
                    # Не помещается до конца дня — считаем заново завтра.
        cursor += timedelta(days=1)

    # Порог длиннее рабочего дня: срок отсчитывается от первого рабочего момента как есть.
    if first_open is not None:
        return (first_open + timedelta(seconds=seconds)).astimezone(timezone.utc)
    return None


# ── История версий графика ──────────────────────────────────────
# Обращение считается по графику, действовавшему В МОМЕНТ ОБРАЩЕНИЯ, — один график
# на всё обращение (срок реакции, срок специалиста, рабочие секунды). По текущему
# графику — только вопросы про «сейчас»: окно тишины алертов, рабочий ли день для рассылок.
# Хранение — в настройке `work_calendar`: поля раздела, `since`, `history`
# (см. versioning); запись версии — в settings_store.set_value.

CALENDAR_FIELDS = ("weekdays", "start", "end", "timezone", "holidays")


def schedule_label(config: dict[str, Any]) -> str:
    from app.services.transcript import WEEKDAYS

    days = sorted(config.get("weekdays") or [1, 2, 3, 4, 5])
    span = f"{WEEKDAYS[days[0] - 1]}–{WEEKDAYS[days[-1] - 1]}" if days else "не задано"
    return f"{config.get('start', '?')}–{config.get('end', '?')}, {span}"


class CalendarHistory(History):
    """График на любой момент прошлого — `History` с календарными подписями."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config, CALENDAR_FIELDS, config.get("timezone"))

    def footnote(self, start: datetime, end: datetime) -> str | None:
        """Сноска к отчёту за период, внутри которого график менялся; None — график был один."""
        spans = self.spans(start, end)
        if len(spans) < 2:
            return None
        tz, *_ = _params(self.current)
        parts: list[str] = []
        for version, since in spans:
            when = (
                f"с {since.astimezone(tz):%d.%m %H:%M}"
                if since is not None
                else f"до {spans[1][1].astimezone(tz):%d.%m %H:%M}"
            )
            parts.append(f"{when} — {schedule_label(version)}")
        return (
            "График в этом периоде менялся: "
            + "; ".join(parts)
            + ". Каждое обращение посчитано по графику, действовавшему "
            "в момент обращения, — прошлое не пересчитывается."
        )


def calendar_at(config: dict[str, Any], moment: datetime | None) -> dict[str, Any]:
    return CalendarHistory(config).at(moment)
