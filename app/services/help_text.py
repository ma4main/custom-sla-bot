"""Короткая справка о действующих правилах учёта.

Функции рендера не обращаются к базе и сети: значения берутся из контекста.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.calendar import schedule_label
from app.services.settings_store import (
    DEFAULT_REACTION_MINUTES,
    DEFAULT_SPECIALIST_MINUTES,
    DEFAULT_WAIT_REACTION_HOURS,
    DEFAULT_WAIT_SPECIALIST_DAYS,
    MODE_LABELS,
    MODE_ON,
    normalize_mode,
)
from app.services.transcript import specialist_deadline_label
from app.text import esc

PAGES: tuple[tuple[str, str], ...] = (
    ("time", "🕐 Сроки"),
    ("episode", "💬 Обращения"),
    ("reports", "📊 Отчёты"),
    ("alerts", "🔔 Уведомления"),
    ("ai", "🤖 Распознавание"),
    ("roles", "👥 Доступ и чаты"),
)
PAGE_TITLES = dict(PAGES)
_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


@dataclass
class HelpContext:
    calendar: dict[str, Any] = field(default_factory=dict)
    alerts: dict[str, Any] = field(default_factory=dict)
    episodes: dict[str, Any] = field(default_factory=dict)
    digest: dict[str, Any] = field(default_factory=dict)
    ai_enabled: bool = True
    notify_group_configured: bool = False
    can_review_authors: bool = False
    can_review_alerts: bool = False


def _tz_label(calendar: dict[str, Any]) -> str:
    tz = str(calendar.get("timezone") or "Europe/Moscow")
    return "Москва" if tz == "Europe/Moscow" else tz


def _specialist_label(alerts: dict[str, Any]) -> str:
    return specialist_deadline_label(
        int(alerts.get("substantive_threshold_minutes") or DEFAULT_SPECIALIST_MINUTES)
    )


def _weekday(value: Any) -> str:
    try:
        index = int(value) - 1
    except (TypeError, ValueError):
        return "день не задан"
    return _WEEKDAYS[index] if 0 <= index < 7 else "день не задан"


def hub(ctx: HelpContext) -> str:
    return (
        "<b>📖 Справка</b>\n\n"
        "Бот следит за скоростью ответов в чатах. "
        "Статус «отвечено» не подтверждает выполнение задачи.\n\n"
        "Выберите раздел ниже."
    )


def _page_time(ctx: HelpContext) -> str:
    calendar, alerts, episodes = ctx.calendar, ctx.alerts, ctx.episodes
    threshold = int(alerts.get("threshold_minutes") or DEFAULT_REACTION_MINUTES)
    holidays = calendar.get("holidays") or []
    holiday_line = (
        f"Отдельных праздничных дат в календаре: {len(holidays)}."
        if holidays else "Отдельные праздники не заданы."
    )
    return (
        "<b>🕐 Сроки</b>\n\n"
        f"<b>Рабочее время:</b> {esc(schedule_label(calendar))}, {esc(_tz_label(calendar))}. "
        f"{holiday_line}\n\n"
        f"<b>Первая реакция:</b> {threshold} рабочих минут. Реакцией считается "
        "сообщение компании, в том числе короткое «принято»; одно приветствие "
        "(«Добрый день») реакцией не считается. Ночью и в выходные "
        "отсчёт ждёт открытия. Если вечером весь порог не помещается, "
        f"утром даются полные {threshold} минут.\n\n"
        f"<b>После передачи специалисту:</b> {esc(_specialist_label(alerts))}; "
        "срок ограничен рабочими часами. Считается выход на связь другого "
        "распознанного сотрудника; содержательный ответ или документ "
        "передавшего также может завершить ожидание.\n\n"
        "<b>Если срок нарушен:</b> бот ещё ждёт "
        f"{int(episodes.get('wait_reaction_hours') or DEFAULT_WAIT_REACTION_HOURS)} ч без реакции или "
        f"{int(episodes.get('wait_specialist_days') or DEFAULT_WAIT_SPECIALIST_DAYS)} календарных дн. "
        "после передачи. Ожидание отсчитывается с начала ближайшего рабочего "
        "времени после срока. Затем — «осталось без ответа».\n\n"
        "Показатели скорости считают рабочее время; календарное ожидание "
        "показывается отдельно. Поздние ответы и уточнения могут изменить "
        "отчёт. График и пороги сохраняются по времени открытия обращения."
    )


def _page_episode(ctx: HelpContext) -> str:
    return (
        "<b>💬 Обращения</b>\n\n"
        "Обращение объединяет сообщения клиента и ответы компании. "
        "Оно относится к дню первого сообщения клиента.\n\n"
        "<b>Ждёт реакции</b> — учитываемой реакции компании ещё нет.\n"
        "<b>После реакции</b> — реакция есть, учёт обращения ещё открыт.\n"
        "<b>Отвечено</b> — бот завершил ожидание по своим правилам. "
        "Без передачи специалисту для этого может хватить «принято».\n"
        "<b>Ответа не требовалось</b> — бот не обнаружил необходимости "
        "отвечать в чате.\n"
        "<b>Осталось без ответа</b> — окно ожидания закончилось без нужной "
        "реакции или контакта специалиста.\n\n"
        "Несколько разных просьб могут объединиться в одно обращение. "
        "Если цифра вызывает сомнение, проверьте переписку: бот не "
        "гарантирует отдельный учёт каждой задачи."
    )


def _page_reports(ctx: HelpContext) -> str:
    author_help = (
        "Неизвестных авторов проверяют в «Сотрудники → Не определили, кто это»; "
        "если бот принял участника чата не за того, откройте переписку и "
        "нажмите «👥 Участники» — там это исправляется одним выбором."
        if ctx.can_review_authors else
        "Если автор ответа определён неверно, сообщите администратору."
    )
    return (
        "<b>📊 Отчёты</b>\n\n"
        "<b>«Обычно»</b> — медианное время: половина ответов быстрее, "
        "половина медленнее. <b>«9 из 10 — быстрее»</b> — ориентир "
        "для наиболее долгого ожидания.\n\n"
        "<b>Просрочка реакции</b> считается от обращения клиента. "
        "<b>Просрочка специалиста</b> — от передачи. Просрочка записывается "
        "на сотрудника, чей ответ был засчитан; случаи без ответившего "
        "сотрудника показываются отдельно.\n\n"
        "Кнопки под отчётом открывают обращения с опозданием, текущим "
        "ожиданием или закончившимся ожиданием.\n\n"
        "<b>«Требует внимания»</b> — текущая очередь, независимо от периода "
        "отчёта. <b>«Снятые нарушения»</b> — решения об исключении нарушения; "
        "решение можно отменить. Доступ к разделам зависит от прав.\n\n"
        "<b>«Последние 7 дней»</b> — семь полных дней, включая вчерашний. "
        "<b>«Прошлая неделя»</b> — понедельник–воскресенье.\n\n"
        + author_help
    )


def _digest_lines(digest: dict[str, Any]) -> list[str]:
    lines = []
    if digest.get("alerts_digest_enabled"):
        times = ", ".join(esc(str(t)) for t in digest.get("alerts_digest_times") or [])
        lines.append(
            f"<b>Сводки по алертам:</b> {times or 'время не задано'}. "
            "Каждый выпуск приходит, даже если алертов и новых итогов нет."
        )
    else:
        lines.append("<b>Сводки по алертам:</b> выключены.")
    if digest.get("weekly_enabled"):
        period = MODE_LABELS.get(str(digest.get("weekly_period") or ""), "период не задан")
        extras = [name for key, name in (("weekly_html", "HTML-страница"), ("weekly_xlsx", "XLSX"))
                  if digest.get(key)]
        attachments = f"; вложение: {' и '.join(extras)}" if extras else ""
        lines.append(
            f"<b>Недельный отчёт:</b> {_weekday(digest.get('weekly_day'))}, "
            f"{esc(str(digest.get('weekly_time') or '?'))}; {esc(period)}{attachments}."
        )
    else:
        lines.append("<b>Недельный отчёт:</b> выключен.")
    lines.append(
        f"<b>Отдельный отчёт за день:</b> каждый рабочий день, {esc(str(digest.get('evening_time') or '?'))} "
        "(настройка «Вечерняя сводка»)."
        if digest.get("evening_enabled") else
        "<b>Отдельный отчёт за день:</b> выключен (настройка «Вечерняя сводка»)."
    )
    lines.append(
        f"<b>Месячный отчёт:</b> {esc(str(digest.get('monthly_day') or '?'))}-го числа, "
        f"{esc(str(digest.get('monthly_time') or '?'))}."
        if digest.get("monthly_enabled") else "<b>Месячный отчёт:</b> выключен."
    )
    return lines


def _page_alerts(ctx: HelpContext) -> str:
    alerts = ctx.alerts
    second = normalize_mode(alerts.get("substantive_mode"))
    mode = "включён" if second == MODE_ON else "выключен"
    status = "Отправка алертов включена" if alerts.get("enabled") else "Отправка алертов выключена"
    when = "в рабочее время" if alerts.get("respect_quiet_hours", True) else "в любое время суток"
    routes = "Личная доставка — по правам и настройкам получателей."
    if alerts.get("to_group") and ctx.notify_group_configured:
        routes += " Включена доставка алертов в группу уведомлений."
    elif alerts.get("to_group"):
        routes += " Группа уведомлений не задана."
    if ctx.digest.get("to_group") and ctx.notify_group_configured:
        routes += " Рассылки также идут в группу."
    review = (
        "Кнопка под алертом открывает переписку; ошибочный случай можно "
        "снять кнопкой «✔️ Снять нарушение»."
        if ctx.can_review_alerts else
        "Если алерт ошибочный, сообщите администратору и укажите переписку."
    )
    return (
        "<b>🔔 Уведомления</b>\n\n"
        "<b>Нет реакции</b> — нарушен срок первой реакции. "
        "<b>Нет ответа специалиста</b> — нарушен срок после передачи. "
        "По каждому виду для обращения — отдельное уведомление, "
        "без регулярных повторов.\n\n"
        f"{status}; время доставки — {when}. Вид «Нет ответа специалиста» {mode}. "
        f"{routes}\n\n"
        "После ответа или пересмотра случая заголовок алерта обновляется. "
        "«Осталось без ответа» не зачёркивается. " + review + "\n\n"
        "При сбое ИИ отправка может приостановиться до восстановления обработки. "
        "Бот пришлёт об этом одно сообщение: что именно случилось "
        "(например, истёк ключ провайдера) и что нужно сделать.\n\n"
        + "\n".join(_digest_lines(ctx.digest))
    )


def _page_ai(ctx: HelpContext) -> str:
    status = "" if ctx.ai_enabled else "⚠️ ИИ сейчас выключен; действуют только простые правила.\n\n"
    return (
        "<b>🤖 Распознавание</b>\n\n" + status
        + "Бот определяет, нужен ли ответ клиенту, и различает реакцию, "
        "встречный вопрос, передачу специалисту и ответ компании.\n\n"
        "Короткие подтверждения обычно не требуют ответа. Ответ на вопрос "
        "сотрудника и договорённость о звонке могут не создавать нового "
        "ожидания в чате.\n\n"
        "Бот использует текст и контекст, но не читает содержимое вложений. "
        "Подписи «в оплату», фото с приветствием, напоминания и несколько "
        "просьб подряд могут быть распознаны неверно.\n\n"
        "Если вывод сомнителен, откройте переписку. Отсутствие алерта само "
        "по себе не означает, что клиенту помогли."
    )


def _page_roles(ctx: HelpContext) -> str:
    return (
        "<b>👥 Доступ и чаты</b>\n\n"
        "<b>Владелец и администратор</b> управляют ботом; передача владения "
        "доступна владельцу. <b>Сотрудник</b> видит свои показатели и "
        "уведомления по своим передачам. Экран «Состояние системы» доступен "
        "владельцу и администратору. Права могут настраиваться отдельно. "
        "Справка доступна всем активным пользователям.\n\n"
        "Роли «менеджер» и «специалист» в аналитике определяются по работе "
        "в чатах и не равны правам доступа к боту.\n\n"
        "<b>В анализе</b> — сообщения учитываются. <b>На паузе</b> — новые "
        "сообщения не учитываются. <b>В архиве</b> — бот удалён из чата. "
        "<b>Обнаружен</b> — чат ещё не включён в анализ. Сохранённая "
        "история остаётся доступной.\n\n"
        "Автор определяется по подписи портала или распознанному аккаунту "
        "сотрудника. Неизвестный автор может повлиять на зачёт ответа "
        "и личные показатели."
    )


_RENDERERS = {
    "time": _page_time, "episode": _page_episode, "reports": _page_reports,
    "alerts": _page_alerts, "ai": _page_ai, "roles": _page_roles,
}


def render(page: str, ctx: HelpContext) -> str:
    renderer = _RENDERERS.get(page)
    return renderer(ctx) if renderer else hub(ctx)
