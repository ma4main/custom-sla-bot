"""Бизнес-настройки: хранение и значения по умолчанию.

Всё, что владелец меняет сам, кнопками (docs/SCREENS.md, раздел 8). Секретов тут нет —
они в переменных окружения.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Setting

# ═══════════════════════════════════════════════════════════════
# Значения по умолчанию
# ═══════════════════════════════════════════════════════════════

# Режимы алерта второй ступени.
MODE_OFF = "off"
MODE_ON = "on"

MODE_LABELS = {
    MODE_OFF: "выключен",
    MODE_ON: "включён",
    # Раскладки рассылаемого отчёта — в общем словаре подписей режимов.
    "full": "полный — со списками чатов и сотрудников",
    "brief": "краткий — только цифры, без списков",
    "prev_week": "прошлая календарная неделя (пн–вс)",
    "last7": "последние 7 дней (включая вчерашний, день отправки не входит)",
}
MODE_CYCLE = (MODE_OFF, MODE_ON)


def normalize_mode(value: Any) -> str:
    """Неизвестное значение читается как «выключен»."""
    text = str(value or "").strip().lower()
    return text if text in MODE_LABELS else MODE_OFF


# Умолчания графика и сроков — один источник: из них собран DEFAULTS, их же берёт
# код, читающий раздел или версию настройки, где ключа нет.
DEFAULT_WORKDAY_START = "10:00"
DEFAULT_WORKDAY_END = "19:00"
DEFAULT_REACTION_MINUTES = 30
DEFAULT_SPECIALIST_MINUTES = 1440
DEFAULT_WAIT_REACTION_HOURS = 24
DEFAULT_WAIT_SPECIALIST_DAYS = 7

DEFAULTS: dict[str, dict[str, Any]] = {
    # Рабочий календарь; праздники и переносы правятся кнопками.
    "work_calendar": {
        "weekdays": [1, 2, 3, 4, 5],  # понедельник = 1
        "start": DEFAULT_WORKDAY_START,
        "end": DEFAULT_WORKDAY_END,
        "timezone": "Europe/Moscow",
        "holidays": [],
        # Версии графика (calendar.CalendarHistory): обращение считается по графику,
        # действовавшему в его момент. `since`/`history` пишет бот, с экрана скрыты.
        "since": None,
        "history": [],
    },
    "alerts": {
        # Срок первой реакции, минуты.
        "threshold_minutes": DEFAULT_REACTION_MINUTES,
        # Вторая ступень: срок ответа специалиста после передачи. Считается не как «+24 часа»,
        # а как «то же время следующего рабочего дня» (calendar.same_time_next_workday).
        "substantive_threshold_minutes": DEFAULT_SPECIALIST_MINUTES,
        # Алерт второй ступени: off | on. Метрика по существу считается всегда.
        "substantive_mode": MODE_OFF,
        # Дублировать алерты в группу уведомлений. Лички работают всегда — группа
        # добавляется к ним, а не вместо них.
        "to_group": False,
        "respect_quiet_hours": True,
        "enabled": False,
        # Версии порогов (см. versioning): служебные поля, пишет бот.
        "since": None,
        "history": [],
    },
    "episodes": {
        # Сколько ждать ПОСЛЕ наступления срока, прежде чем перестать ждать. Часы
        # КАЛЕНДАРНЫЕ: рабочий календарь уже учтён в самом сроке (episodes.expired_at).
        "wait_reaction_hours": DEFAULT_WAIT_REACTION_HOURS,
        "wait_specialist_days": DEFAULT_WAIT_SPECIALIST_DAYS,
        # Верхняя граница сообщений в эпизоде: иначе частые сообщения растянут один
        # эпизод на весь день и испортят медиану и p90.
        "max_messages": 50,
        # Версии окон — как у порогов алертов.
        "since": None,
        "history": [],
    },
    "chats": {
        # Добавили бота в чат — чат анализируется без ручного включения. Выключено —
        # новые чаты ждут подтверждения в состоянии «обнаружен».
        "auto_track_new": True,
    },
    "digest": {
        # Вечерняя сводка дня; выключена по умолчанию.
        "evening_enabled": False,
        # После конца рабочего дня; в нерабочие дни сводка молчит.
        "evening_time": "19:05",
        # Еженедельный отчёт: текст плюс вложения. День 1 = понедельник … 7 = воскресенье;
        # попал на праздник — уходит в первый рабочий день после.
        "weekly_enabled": False,
        "weekly_day": 1,
        "weekly_time": "10:05",
        # Период: прошлая календарная неделя (пн–вс) или последние 7 завершённых дней
        # (без дня отправки).
        "weekly_period": "prev_week",
        # HTML-страница вложением — тот же файл, что кнопка «📄 HTML» под сводным отчётом.
        "weekly_html": False,
        "weekly_xlsx": True,
        # Ежемесячный отчёт за прошлый календарный месяц. День 1–28 (29–31 есть не в каждом
        # месяце); выходной или праздник — уходит первым рабочим днём после.
        "monthly_enabled": False,
        "monthly_day": 1,
        "monthly_time": "10:05",
        "monthly_html": False,
        "monthly_xlsx": True,
        # Сводки по алертам: закрыт ли алерт и какой ценой.
        "alerts_digest_enabled": False,
        # От одной до пяти сводок в день, «ЧЧ:ММ» по возрастанию. Окно каждой сводки —
        # с прошлого выпуска; до первого выпуска — со вчерашней полуночи.
        "alerts_digest_times": ["10:10", "14:30", "18:30"],
        # Состав отчёта: «полный» — со списками чатов и сотрудников, «краткий» — только цифры.
        # Общий для недели и месяца.
        "report_layout": "full",
        # В группу уведомлений — дополнительно к личкам, как у алертов. Общий на все рассылки.
        "to_group": False,
    },
    "retention": {
        "raw_update_days": 90,
        "message_text_days": None,  # бессрочно — тексты нужны для пересчёта атрибуции
    },
}

# Группировка полей раздела на экране: (заголовок блока, ключи). Ключи вне блоков
# показываются после блоков.
FIELD_GROUPS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "digest": (
        ("1️⃣ Вечерняя сводка — каждый рабочий день", ("evening_enabled", "evening_time")),
        (
            "7️⃣ Недельный отчёт — раз в неделю",
            (
                "weekly_enabled",
                "weekly_day",
                "weekly_time",
                "weekly_period",
                "weekly_html",
                "weekly_xlsx",
            ),
        ),
        (
            "3️⃣0️⃣ Месячный отчёт — раз в месяц",
            ("monthly_enabled", "monthly_day", "monthly_time", "monthly_html", "monthly_xlsx"),
        ),
        (
            "🧾 Сводки по алертам — несколько раз в рабочий день",
            ("alerts_digest_enabled", "alerts_digest_times"),
        ),
        ("📤 Общее для отчётов", ("report_layout", "to_group")),
    ),
}


def grouped_section(section: str) -> bool:
    """Разделы с именованными блоками показываются хабом: сперва кнопки блоков,
    поля — внутри блока.
    """
    return section in FIELD_GROUPS


def group_index_of(section: str, key: str) -> int | None:
    for index, (_, keys) in enumerate(FIELD_GROUPS.get(section, ())):
        if key in keys:
            return index
    return None


def section_groups(
    section: str, shown_keys: list[str]
) -> list[tuple[str | None, list[str]]]:
    """Блоки раздела: (заголовок | None, ключи) в порядке показа. Без групп — один
    безымянный блок. Скрытые ключи (SIMPLIFIED_AWAY и т. п.) выпадают и из блока.
    """
    groups = FIELD_GROUPS.get(section)
    if not groups:
        return [(None, list(shown_keys))]

    result: list[tuple[str | None, list[str]]] = []
    used: set[str] = set()
    for title, keys in groups:
        present = [key for key in keys if key in shown_keys]
        used.update(present)
        if present:
            result.append((title, present))
    leftover = [key for key in shown_keys if key not in used]
    if leftover:
        result.append((None, leftover))
    return result


_LABELS = {
    "work_calendar": "🕐 Рабочий календарь",
    "alerts": "🔔 Алерты",
    "digest": "📮 Отчёты по расписанию",
    "episodes": "📈 Обращения",
    "chats": "💬 Чаты",
    "retention": "🗄 Хранение",
}

# Русские подписи параметров. Технический ключ виден рядом (он же в журнале действий).
FIELD_LABELS: dict[str, dict[str, str]] = {
    "work_calendar": {
        "weekdays": "Рабочие дни недели (1=пн … 7=вс)",
        "start": "Начало рабочего дня",
        "end": "Конец рабочего дня",
        "timezone": "Часовой пояс",
        "holidays": "Праздники (даты)",
    },
    "alerts": {
        "threshold_minutes": "Порог первой реакции, мин",
        "substantive_threshold_minutes": "Срок ответа специалиста после передачи, мин",
        "substantive_mode": "Алерты «нет ответа специалиста»",
        "to_group": "Алерты в группу уведомлений",
        "respect_quiet_hours": "Уведомления только в рабочее время",
        "enabled": "Отправка алертов",
    },
    "episodes": {
        "wait_reaction_hours": "Ждём реакции после просрочки, часов",
        "wait_specialist_days": "Ждём ответа специалиста после просрочки, дней",
        "max_messages": "Макс. сообщений в обращении",
    },
    "chats": {
        "auto_track_new": "Автовключение новых чатов",
    },
    "digest": {
        "evening_enabled": "Вечерняя сводка",
        "evening_time": "Время вечерней сводки",
        "weekly_enabled": "Еженедельный отчёт",
        "weekly_day": "День недельного отчёта (1=пн … 7=вс)",
        "weekly_time": "Время недельного отчёта",
        "weekly_period": "Период недельного отчёта",
        "weekly_html": "HTML-страница к недельному отчёту",
        "weekly_xlsx": "XLSX-файл к недельному отчёту",
        "monthly_xlsx": "XLSX-файл к месячному отчёту",
        "monthly_enabled": "Ежемесячный отчёт",
        "monthly_day": "День месячного отчёта (1–28)",
        "monthly_time": "Время месячного отчёта",
        "monthly_html": "HTML-страница к месячному отчёту",
        "alerts_digest_enabled": "Сводки по алертам",
        "alerts_digest_times": "Времена сводок по алертам",
        "report_layout": "Состав рассылаемого отчёта",
        "to_group": "Слать и в группу уведомлений",
    },
    "retention": {
        "raw_update_days": "Хранить сырые апдейты, дней",
        "message_text_days": "Хранить тексты сообщений, дней",
    },
}


def section_label(key: str) -> str:
    return _LABELS.get(key, key)


def field_label(section: str, key: str) -> str:
    if key == "since":
        # Служебное поле версии скрыто с экрана, но в журнале действий печатается по-русски.
        return "Действует с"
    return FIELD_LABELS.get(section, {}).get(key, key)


# Краткое описание раздела наверху его экрана.
SECTION_DESCRIPTIONS: dict[str, str] = {
    "work_calendar": (
        "Рабочие дни и часы компании. По этому календарю считаются сроки "
        "ответов, просрочки и время рассылок: вне рабочего времени бот молчит."
    ),
    "alerts": (
        "Уведомления о просрочках. Приходят владельцам в личные сообщения (и в группу "
        "уведомлений, если включено) и только в рабочее время. Ступень 1 — "
        "клиенту не ответили вовремя; ступень 2 — специалист не ответил после "
        "передачи вопроса."
    ),
    "digest": (
        "Автоматические отчёты по расписанию.\n\n"
        "📊 Вечерняя сводка — итоги дня после конца рабочего дня.\n"
        "📈 Недельный отчёт — сводный за прошлую неделю или за последние "
        "7 дней (настройка периода), в выбранный день.\n"
        "📅 Месячный отчёт — сводный за прошлый месяц, в выбранное число.\n"
        "🧾 Сводки по алертам — несколько раз в рабочий день: что стало "
        "с каждым алертом (ответили, сняли решением, всё ещё горит).\n\n"
        "Состав отчёта: полный (со списками) или краткий (только цифры).\n"
        "Получают те же, кто получает алерты; флажок «в группу уведомлений» "
        "добавляет к ним группу."
    ),
    "episodes": (
        "Обращение — вопрос клиента и всё, что было ответом.\n\n"
        "Сколько ждать — отдельно для двух ступеней, и отсчёт идёт "
        "с момента, когда срок УЖЕ нарушен. Перестали ждать — обращение "
        "уходит в «остались без ответа», а следующее сообщение клиента "
        "открывает новое обращение. Ответят позже — обращение снова "
        "станет отвеченным: бот пересчитывает всё заново каждую минуту."
    ),
    "retention": (
        "Сколько хранить данные. Сырые апдейты — полный технический журнал "
        "всего, что прислал Telegram (страховка: из него всё пересчитывается); "
        "чистится по сроку. Тексты сообщений — сама переписка: нужна для "
        "выписок и пересчётов, по умолчанию хранится бессрочно."
    ),
}


def section_description(key: str) -> str | None:
    return SECTION_DESCRIPTIONS.get(key)


def version_label(section: str, version: dict[str, Any]) -> str:
    if section == "work_calendar":
        from app.services.calendar import schedule_label

        return schedule_label(version)
    if section == "alerts":
        return (
            f"реакция {version.get('threshold_minutes')} мин, "
            f"специалист {version.get('substantive_threshold_minutes')} мин"
        )
    if section == "episodes":
        return (
            f"ждём {version.get('wait_reaction_hours')} ч после срока реакции, "
            f"{version.get('wait_specialist_days')} дн после срока специалиста"
        )
    return ", ".join(
        f"{key}={version.get(key)}" for key in versioned_fields(section)
    )


_HISTORY_TITLES = {
    "work_calendar": "История графика",
    "alerts": "История порогов",
    "episodes": "История окон ожидания",
}


def section_extra(section: str, values: dict[str, Any]) -> str | None:
    """Текст под полями раздела: история версий, строка «действует» последней.
    Без истории непонятно, почему прошлое не пересчиталось после правки.
    """
    fields = versioned_fields(section)
    if not fields:
        return None

    from app.services.transcript import calendar_tz
    from app.services.versioning import History

    history = History(values, fields, values.get("timezone"))
    title = _HISTORY_TITLES.get(section, "История настроек")
    if not history.changed():
        return (
            "<i>Эти настройки участвуют в расчёте сроков и хранят историю. "
            "Пока не менялись. После изменения прошлые обращения останутся "
            "посчитанными по прежним правилам — отчёты за прошедшие периоды "
            "не изменятся.</i>"
        )

    # Пояс — рабочего календаря: своего пояса у алертов и обращений нет.
    tz = calendar_tz(values if section == "work_calendar" else {})
    lines = [f"<b>{title}</b>"]
    for index, version in enumerate(history.versions):
        since = version["since"]
        when = (
            f"с {since.astimezone(tz):%d.%m.%Y %H:%M}"
            if since is not None
            else "до первого изменения"
        )
        mark = " — действует" if index == len(history.versions) - 1 else ""
        lines.append(f"• {when}: {version_label(section, version)}{mark}")
    lines.append(
        "<i>Обращение считается по правилам, действовавшим в момент "
        "обращения: смена настройки не пересчитывает прошлое. Момент, "
        "с которого действует нынешняя версия, можно поправить кнопкой "
        "«🕐 Действует с …».</i>"
    )
    return "\n".join(lines)


# Подсказка формата для формы ввода конкретного поля.
FIELD_HINTS: dict[str, dict[str, str]] = {
    "work_calendar": {
        "weekdays": (
            "Дни недели числами через запятую: 1 — понедельник … 7 — воскресенье.\n"
            "Пример: <code>1,2,3,4,5</code>"
        ),
        "start": "Время в формате ЧЧ:ММ. Пример: <code>10:00</code>",
        "end": "Время в формате ЧЧ:ММ. Пример: <code>19:00</code>",
        "timezone": "Название часового пояса. Пример: <code>Europe/Moscow</code>",
        "holidays": (
            "Даты через запятую в формате ГГГГ-ММ-ДД, диапазон — через дефис.\n"
            "Пример: <code>2027-01-01 - 2027-01-08, 2027-02-23, 2027-03-08</code>"
        ),
    },
    "episodes": {
        "wait_reaction_hours": (
            "Календарных часов. Пример: <code>24</code>\n\n"
            "Отсчёт идёт с момента, когда срок УЖЕ нарушен, а сам срок "
            "считается по рабочему календарю.\n"
            "• Клиент написал во вторник в 15:00 — срок 15:30, ждём "
            "до среды 15:30.\n"
            "• Написал в пятницу в 16:50 — 30 минут до конца дня не "
            "помещаются, срок переезжает на понедельник 10:30, ждём "
            "до вторника 10:30. Ночь, выходные и праздники окно не съедают."
        ),
        "wait_specialist_days": (
            "Календарных суток. Пример: <code>7</code>\n\n"
            "То же правило для второй ступени: отсчёт с момента, когда "
            "специалист уже просрочил (его срок — то же время следующего "
            "рабочего дня после передачи вопроса)."
        ),
    },
    "alerts": {
        "threshold_minutes": "Число минут. Пример: <code>30</code>",
        "substantive_threshold_minutes": (
            "Число минут; сутки — <code>1440</code>. Срок считается как «то же "
            "время следующего рабочего дня» от момента передачи."
        ),
    },
    "digest": {
        "evening_time": "Время в формате ЧЧ:ММ. Пример: <code>19:05</code>",
        "alerts_digest_times": (
            "Сколько времён укажете — столько сводок в день: одно время — "
            "одна сводка, три времени — три. Формат ЧЧ:ММ через запятую, "
            "от одной до пяти.\n"
            "Пример: <code>10:10, 14:30, 18:30</code>\n\n"
            "<b>Что попадает в сводку</b>\n"
            "• всё, что ещё горит, — любой давности, в каждой сводке;\n"
            "• закрытые алерты — один раз, те, что закрылись с прошлой "
            "сводки.\n"
            "Список того, что горит сейчас, — «Отчёты → Требует внимания».\n\n"
            "Сводки уходят только в рабочие дни. Время после конца "
            "рабочего дня указывать можно: окно тишины алертов на сводки "
            "не распространяется.\n\n"
            "Новое расписание начинает действовать со следующего времени — "
            "сводка не придёт в ту же минуту оттого, что вы поменяли "
            "настройку. Нужна прямо сейчас — кнопка «🧾 Сводка по алертам» "
            "в «Отчётах»."
        ),
        "weekly_time": "Время в формате ЧЧ:ММ. Пример: <code>10:05</code>",
        "weekly_day": (
            "Число от 1 до 7: 1 — понедельник … 7 — воскресенье. Если день "
            "выпадет на праздник, отчёт уйдёт в ближайший рабочий."
        ),
        "monthly_time": "Время в формате ЧЧ:ММ. Пример: <code>10:05</code>",
        "monthly_day": (
            "Число месяца от 1 до 28 (дальше 28-го есть не в каждом месяце). "
            "Если день выпадет на выходной или праздник, отчёт уйдёт первым "
            "рабочим днём после."
        ),
    },
    "retention": {
        "raw_update_days": "Число дней. Пример: <code>90</code>",
        "message_text_days": (
            "Число дней либо слово <code>бессрочно</code>. Тексты нужны для "
            "выписок и пересчётов — сокращать срок стоит только ради приватности."
        ),
    },
}


def field_hint(section: str, key: str) -> str:
    return FIELD_HINTS.get(section, {}).get(
        key,
        "Числа — просто цифрами. Списки — через запятую.",
    )


# Работающие настройки, убранные из интерфейса: значения действуют,
# вернуть поле — одна строка здесь.
SIMPLIFIED_AWAY: dict[str, frozenset[str]] = {
    # Версии графика пишет бот; история выводится текстом (`section_extra`).
    "work_calendar": frozenset({"since", "history"}),
    # Чтобы чат не анализировался, бота в него не добавляют; служебная группа
    # исключена кодом.
    "chats": frozenset({"auto_track_new"}),
    # Технический предел эпизода и служебная память версий (VERSIONED_FIELDS).
    "episodes": frozenset({"max_messages", "since", "history"}),
    "alerts": frozenset({"since", "history"}),
}


def hidden_fields(section: str) -> frozenset[str]:
    return SIMPLIFIED_AWAY.get(section, frozenset())


def visible_fields(section: str, values: dict[str, Any]) -> list[str]:
    """Поля раздела на экране. Ключ, которого нет в DEFAULTS (в сохранённом значении
    может остаться от прежних версий), не показывается и не правится."""
    known = DEFAULTS.get(section, {})
    hidden = hidden_fields(section)
    return [key for key in values if key in known and key not in hidden]


def visible_sections_only(sections: list[str]) -> list[str]:
    return [
        key
        for key in sections
        if any(field not in hidden_fields(key) for field in DEFAULTS.get(key, {}))
    ]


# Поля-перечисления правятся нажатием, а не вводом текста.
CYCLE_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "alerts": {"substantive_mode": MODE_CYCLE},
    "digest": {
        "report_layout": ("full", "brief"),
        "weekly_period": ("prev_week", "last7"),
    },
}


def cycle_values(section: str, key: str) -> tuple[str, ...] | None:
    return CYCLE_FIELDS.get(section, {}).get(key)


def next_in_cycle(section: str, key: str, current: Any) -> str | None:
    values = cycle_values(section, key)
    if not values:
        return None
    try:
        index = values.index(normalize_mode(current))
    except ValueError:
        index = -1
    return values[(index + 1) % len(values)]


async def get_section(session: AsyncSession, key: str) -> dict[str, Any]:
    stored = await session.get(Setting, key)
    base = dict(DEFAULTS.get(key, {}))
    if stored is not None and isinstance(stored.value, dict):
        base.update(stored.value)
    return base


async def get_value(session: AsyncSession, section: str, field: str) -> Any:
    return (await get_section(session, section)).get(field)


# Настройки, участвующие в расчёте сроков: у них история версий, и обращение
# считается по значениям, действовавшим В ЕГО МОМЕНТ (иначе правка переписала бы прошлые отчёты).
VERSIONED_FIELDS: dict[str, tuple[str, ...]] = {
    "work_calendar": ("weekdays", "start", "end", "timezone", "holidays"),
    "alerts": ("threshold_minutes", "substantive_threshold_minutes"),
    "episodes": ("wait_reaction_hours", "wait_specialist_days"),
}


def versioned_fields(section: str) -> tuple[str, ...]:
    return VERSIONED_FIELDS.get(section, ())


def is_versioned(section: str, field: str) -> bool:
    return field in VERSIONED_FIELDS.get(section, ())


async def set_value(
    session: AsyncSession,
    section: str,
    field: str,
    value: Any,
    *,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = await get_section(session, section)
    previous = current.get(field)

    # Правка такой настройки заводит новую версию с этой минуты. Значение не изменилось —
    # версии нет.
    if is_versioned(section, field) and previous != value:
        from app.services.versioning import push_version

        push_version(
            current, versioned_fields(section), now or datetime.now(timezone.utc)
        )

    current[field] = value

    stored = await session.get(Setting, section)
    if stored is None:
        session.add(Setting(key=section, value=current, updated_by=actor_id))
    else:
        stored.value = current
        stored.updated_by = actor_id

    session.add(
        AuditLog(
            actor_user_id=actor_id,
            action="setting.changed",
            object_type="setting",
            object_id=section,
            payload={"field": field, "from": previous, "to": value},
        )
    )
    return current


def previous_version_since(section: str, values: dict[str, Any]):
    """Момент начала ПРЕДЫДУЩЕЙ версии — раньше него нынешнюю не сдвинуть.
    None — предыдущей версии нет.
    """
    from app.services.versioning import History

    history = History(values, versioned_fields(section), values.get("timezone"))
    if len(history.versions) < 2:
        return None
    return history.versions[-2]["since"]


async def set_version_since(
    session: AsyncSession,
    section: str,
    moment: datetime,
    *,
    actor_id: int | None = None,
) -> dict[str, Any]:
    """Передвинуть момент, с которого действует нынешняя версия раздела
    («этот порог действует со вторника»).
    """
    current = await get_section(session, section)
    previous = current.get("since")
    current["since"] = moment.isoformat()

    stored = await session.get(Setting, section)
    if stored is None:
        session.add(Setting(key=section, value=current, updated_by=actor_id))
    else:
        stored.value = current
        stored.updated_by = actor_id

    session.add(
        AuditLog(
            actor_user_id=actor_id,
            action="setting.changed",
            object_type="setting",
            object_id=section,
            payload={"field": "since", "from": previous, "to": current["since"]},
        )
    )
    return current
