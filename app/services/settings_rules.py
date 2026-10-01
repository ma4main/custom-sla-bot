"""Проверка значений настроек перед записью.

Ошибка возвращается человеку текстом, значение НЕ записывается: настройка с мусором
(несуществующий пояс, рабочий день «с 19:00 до 10:00») ломает систему тихо.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.settings_store import normalize_mode

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SettingError(ValueError):
    """Значение не годится. Текст показывается пользователю как есть."""


def _as_int(value: Any, *, low: int, high: int, what: str) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise SettingError(f"{what}: нужно целое число") from None
    if not low <= number <= high:
        raise SettingError(f"{what}: допустимо от {low} до {high}, получено {number}")
    return number


def _as_bool(value: Any, *, what: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"да", "yes", "true", "1", "вкл", "включено"}:
        return True
    if text in {"нет", "no", "false", "0", "выкл", "выключено"}:
        return False
    raise SettingError(f"{what}: ответьте «да» или «нет»")


def _as_time(value: Any, *, what: str) -> str:
    text = str(value).strip()
    if not _HHMM.match(text):
        raise SettingError(f"{what}: время в формате ЧЧ:ММ, например 10:00")
    return text


def _as_weekdays(value: Any) -> list[int]:
    raw = value if isinstance(value, list) else str(value).split(",")
    days: list[int] = []
    for item in raw:
        day = _as_int(item, low=1, high=7, what="День недели")
        if day not in days:
            days.append(day)
    if not days:
        raise SettingError(
            "Рабочие дни: список не может быть пустым — иначе рабочего времени "
            "не останется вовсе и алерты замолчат навсегда"
        )
    return sorted(days)


def _as_timezone(value: Any) -> str:
    text = str(value).strip()
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise SettingError(
            f"Часовой пояс «{text}» не найден. Пример: Europe/Moscow"
        ) from None
    return text


# «2027-01-01 - 2027-01-08»; «..» и длинные тире тоже принимаются.
_DATE_RANGE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s*(?:\.\.|—|–|-)\s*(\d{4}-\d{2}-\d{2})$"
)
# Предохранитель от опечатки в годе: иначе календарь станет сплошным праздником.
_MAX_RANGE_DAYS = 62


def _one_date(text: str) -> date:
    if not _ISO_DATE.match(text):
        raise SettingError(f"Праздник «{text}»: дата в формате ГГГГ-ММ-ДД")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise SettingError(f"Праздник «{text}»: такой даты не существует") from None


def _as_holidays(value: Any) -> list[str]:
    from datetime import timedelta

    raw = value if isinstance(value, list) else str(value).split(",")
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        span = _DATE_RANGE.match(text)
        if span:
            first, last = _one_date(span.group(1)), _one_date(span.group(2))
            if first > last:
                first, last = last, first
            if (last - first).days > _MAX_RANGE_DAYS:
                raise SettingError(
                    f"Диапазон «{text}» длиннее {_MAX_RANGE_DAYS} дней — похоже "
                    "на опечатку в дате"
                )
            cursor = first
            while cursor <= last:
                iso = cursor.isoformat()
                if iso not in result:
                    result.append(iso)
                cursor += timedelta(days=1)
            continue
        iso = _one_date(text).isoformat()
        if iso not in result:
            result.append(iso)
    return sorted(result)


def _as_days_or_forever(value: Any) -> int | None:
    text = str(value).strip().lower()
    if text in {"", "бессрочно", "никогда", "-", "нет", "0"}:
        return None
    return _as_int(text, low=1, high=3650, what="Срок хранения")


def _group_flag(what: str) -> Callable[[Any], bool]:
    """Флаг «слать в группу»: включить можно, только если группа задана —
    иначе «да» молча ничего не делало бы.
    """

    def _validate(value: Any) -> bool:
        enabled = _as_bool(value, what=what)
        if enabled:
            # Действующий номер: после переезда группы env может отставать.
            from app.config import effective_notify_group_id

            if effective_notify_group_id() is None:
                raise SettingError(
                    "Группа уведомлений не задана в конфигурации сервера "
                    "(NOTIFY_GROUP_CHAT_ID) — включать нечего. Это делает администратор сервера."
                )
        return enabled

    return _validate


_ALERT_MODES = ("off", "on")
_LAYOUTS = ("full", "brief")


def _as_mode(value: Any) -> str:
    # Явный набор, а не MODE_LABELS: словарь подписей общий с раскладками отчёта.
    mode = str(value).strip().lower()
    if mode not in _ALERT_MODES:
        raise SettingError(f"Режим: допустимо {', '.join(_ALERT_MODES)}")
    return normalize_mode(mode)


def _as_layout(value: Any) -> str:
    layout = str(value).strip().lower()
    if layout not in _LAYOUTS:
        raise SettingError("Состав отчёта: допустимо full (полный) или brief (краткий)")
    return layout


_MOMENT = re.compile(
    r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?[\s,]+([01]?\d|2[0-3]):([0-5]\d)$"
)


def as_version_moment(value: Any, *, now: date, tzinfo) -> Any:
    """Момент «с какого числа действует версия настроек»: «ДД.ММ ЧЧ:ММ» (текущий год),
    «ДД.ММ.ГГГГ ЧЧ:ММ», «сейчас», «с начала дня». Будущее отклоняется.

    `now` задаёт только год для записи без года; «сейчас», начало дня и проверка
    на будущее берутся по текущим часам в поясе `tzinfo`.
    """
    from datetime import datetime as _dt, timedelta as _td

    text = str(value).strip().lower()
    moment_now = _dt.now(tzinfo)
    if text in {"сейчас", "now", "-"}:
        return moment_now
    if text in {"с начала дня", "начало дня", "сегодня"}:
        return moment_now.replace(hour=0, minute=0, second=0, microsecond=0)

    match = _MOMENT.match(text)
    if not match:
        raise SettingError(
            "Момент в формате ДД.ММ ЧЧ:ММ (например <code>03.09 10:00</code>) "
            "или слова «сейчас» / «с начала дня»"
        )
    day, month, year, hour, minute = match.groups()
    try:
        moment = _dt(
            int(year) if year else now.year,
            int(month),
            int(day),
            int(hour),
            int(minute),
            tzinfo=tzinfo,
        )
    except ValueError:
        raise SettingError("Такой даты не существует") from None

    # Без года «31.12 10:00» в январе — конец прошедшего года.
    if not year and moment > moment_now + _td(days=1):
        moment = moment.replace(year=moment.year - 1)
    if moment > moment_now:
        raise SettingError(
            "Момент в будущем: версия настроек начинает действовать не позже "
            "«сейчас». Задним числом — можно"
        )
    return moment


_MAX_DIGEST_TIMES = 5


def _as_digest_times(value: Any) -> list[str]:
    """Времена сводок: 1–5 штук «ЧЧ:ММ»; сортируем сами, повтор — ошибка."""
    raw = value if isinstance(value, list) else str(value).split(",")
    times: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        moment = _as_time(text, what="Время сводки")
        if moment in times:
            raise SettingError(f"Время {moment} указано дважды — уберите повтор")
        times.append(moment)
    if not times:
        raise SettingError(
            "Времена сводок: нужно хотя бы одно время. Чтобы сводок не было "
            "вовсе, выключите их кнопкой «Сводки по алертам»"
        )
    if len(times) > _MAX_DIGEST_TIMES:
        raise SettingError(
            f"Времена сводок: не больше {_MAX_DIGEST_TIMES} в день, "
            f"получено {len(times)}"
        )
    return sorted(times)


_WEEKLY_PERIODS = ("prev_week", "last7")


def _as_weekly_period(value: Any) -> str:
    period = str(value).strip().lower()
    if period not in _WEEKLY_PERIODS:
        raise SettingError(
            "Период недельного отчёта: prev_week (прошлая неделя пн–вс) "
            "или last7 (последние 7 дней)"
        )
    return period


# Правило на каждое поле, которое видит пользователь; полей без правила быть не должно.
RULES: dict[str, dict[str, Callable[[Any], Any]]] = {
    "work_calendar": {
        "weekdays": _as_weekdays,
        "start": lambda v: _as_time(v, what="Начало дня"),
        "end": lambda v: _as_time(v, what="Конец дня"),
        "timezone": _as_timezone,
        "holidays": _as_holidays,
    },
    "alerts": {
        "threshold_minutes": lambda v: _as_int(v, low=1, high=1440, what="Порог реакции"),
        # До недели: срок специалиста можно растянуть больше суток.
        "substantive_threshold_minutes": lambda v: _as_int(
            v, low=1, high=10080, what="Срок ответа специалиста"
        ),
        "substantive_mode": _as_mode,
        "to_group": _group_flag("Алерты в группу уведомлений"),
        "respect_quiet_hours": lambda v: _as_bool(v, what="Уведомления только в рабочее время"),
        "enabled": lambda v: _as_bool(v, what="Отправка алертов"),
    },
    "episodes": {
        "wait_reaction_hours": lambda v: _as_int(
            v, low=1, high=720, what="Ждём реакции после просрочки"
        ),
        "wait_specialist_days": lambda v: _as_int(
            v, low=1, high=60, what="Ждём ответа специалиста после просрочки"
        ),
        "max_messages": lambda v: _as_int(v, low=1, high=1000, what="Сообщений в обращении"),
    },
    "chats": {
        "auto_track_new": lambda v: _as_bool(v, what="Автовключение новых чатов"),
    },
    "digest": {
        "evening_enabled": lambda v: _as_bool(v, what="Вечерняя сводка"),
        "evening_time": lambda v: _as_time(v, what="Время вечерней сводки"),
        "weekly_enabled": lambda v: _as_bool(v, what="Еженедельный отчёт"),
        "weekly_day": lambda v: _as_int(v, low=1, high=7, what="День недельного отчёта"),
        "weekly_time": lambda v: _as_time(v, what="Время недельного отчёта"),
        "weekly_html": lambda v: _as_bool(v, what="HTML-страница к недельному отчёту"),
        "weekly_period": _as_weekly_period,
        "weekly_xlsx": lambda v: _as_bool(v, what="XLSX-файл к недельному отчёту"),
        "monthly_xlsx": lambda v: _as_bool(v, what="XLSX-файл к месячному отчёту"),
        "monthly_enabled": lambda v: _as_bool(v, what="Ежемесячный отчёт"),
        # До 28-го: 29–31 бывают не в каждом месяце.
        "monthly_day": lambda v: _as_int(v, low=1, high=28, what="День месячного отчёта"),
        "monthly_time": lambda v: _as_time(v, what="Время месячного отчёта"),
        "monthly_html": lambda v: _as_bool(v, what="HTML-страница к месячному отчёту"),
        "alerts_digest_enabled": lambda v: _as_bool(v, what="Сводки по алертам"),
        "alerts_digest_times": _as_digest_times,
        "report_layout": _as_layout,
        "to_group": _group_flag("Слать и в группу уведомлений"),
    },
    "retention": {
        "raw_update_days": lambda v: _as_int(
            v, low=1, high=3650, what="Хранение сырых апдейтов"
        ),
        # None — бессрочно (тексты нужны для пересборки атрибуции).
        "message_text_days": _as_days_or_forever,
    },
}


def validate(section: str, key: str, value: Any) -> Any:
    rule = RULES.get(section, {}).get(key)
    if rule is None:
        raise SettingError("Эту настройку менять нельзя")
    return rule(value)


def check_section(section: str, values: dict[str, Any]) -> None:
    if section != "work_calendar":
        return
    start, end = values.get("start"), values.get("end")
    if start and end and str(start) >= str(end):
        raise SettingError(
            f"Рабочий день с {start} до {end} пуст: конец должен быть позже начала, "
            "иначе рабочего времени не останется и алерты замолчат"
        )
