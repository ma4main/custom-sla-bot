"""HTML-отчёт одним файлом — приложение к недельной и месячной рассылке.

Страница самодостаточна: без шрифтов, библиотек, JS и запросов наружу,
открывается с диска. Отчёт — про период; открытые сейчас обращения не входят.
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.report_data import load_speed, load_summary
from app.services.report_lab import (
    after_hours,
    load_profile,
    problem_chats,
    staff_speed,
)
from app.services.staff_roles import ROLE_MANAGER, ROLE_SPECIALIST
from app.services.transcript import WEEKDAYS, fmt_duration, specialist_deadline_label
from app.text import esc

_EXPORT_DIR = Path("/tmp/exports")

# Цвет никогда не единственный носитель смысла: рядом с тоном всегда число или слово.
_CSS = """
:root {
  --bg: #f8fafb; --surface: #fff; --text: #17212b; --muted: #66727e;
  --line: #d7dde2;
  --blue: #2563a8; --blue-soft: #eaf2fb;
  --green: #207a55; --green-soft: #e8f5ee;
  --amber: #9a6500; --amber-soft: #fff3d2;
  --red: #b3262e; --red-soft: #fdebed;
  --yellow: #eab308;
}
* { box-sizing: border-box; }
body {
  background: var(--bg); color: var(--text); margin: 0;
  padding: 32px 20px 48px;
  font: 15px/1.55 "Segoe UI", -apple-system, Arial, sans-serif;
}
main { max-width: 960px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: 14px; margin: 0 0 28px; }
section { margin: 0 0 30px; }
h2 {
  font-size: 15px; letter-spacing: .04em; text-transform: uppercase;
  color: var(--blue); margin: 0 0 12px;
  padding-left: 10px; border-left: 3px solid var(--blue);
}
h3 { margin: 18px 0 8px; font-size: 14px; color: var(--text); }
.cards {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
}
.card {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: 8px; padding: 12px 14px;
}
.card .label { color: var(--muted); font-size: 12.5px; margin-bottom: 4px; }
.card .value { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }
.card .value .unit { font-size: 13px; color: var(--muted); font-weight: 400; }
.card.good { border-color: var(--green); background: var(--green-soft); }
.card.good .value { color: var(--green); }
.card.warn { border-color: var(--amber); background: var(--amber-soft); }
.card.warn .value { color: var(--amber); }
.card.bad { border-color: var(--red); background: var(--red-soft); }
.card.bad .value { color: var(--red); }
.note { color: var(--muted); font-size: 13px; margin-top: 10px; }
.legend {
  background: var(--blue-soft); border: 1px solid var(--line);
  border-radius: 8px; padding: 12px 16px; margin-top: 12px; font-size: 13.5px;
}
.legend p { margin: 4px 0; }
.legend b { color: var(--blue); }
.scroll { overflow-x: auto; }
table {
  border-collapse: collapse; width: 100%; background: var(--surface);
  border: 1px solid var(--line); border-radius: 8px; overflow: hidden;
  font-size: 14px;
}
caption {
  caption-side: top; text-align: left; color: var(--muted);
  font-size: 13px; padding: 0 0 8px;
}
th, td { padding: 9px 12px; text-align: left; }
thead th {
  background: var(--blue-soft); color: var(--blue);
  font-size: 12.5px; letter-spacing: .02em;
}
tbody td { border-top: 1px solid var(--line); font-variant-numeric: tabular-nums; }
tbody tr:hover td { background: var(--bg); }
th.num, td.num { text-align: right; }
.pill {
  display: inline-block; border-radius: 20px; padding: 1px 9px;
  font-size: 12.5px; font-weight: 600;
}
.pill.bad { background: var(--red-soft); color: var(--red); }
.pill.good { background: var(--green-soft); color: var(--green); }
.pill.warn { background: var(--amber-soft); color: var(--amber); }
.split { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 640px) { .split { grid-template-columns: 1fr; } }
.panel {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: 8px; padding: 14px 16px;
}
.panel h3 { margin: 0 0 10px; font-size: 13.5px; color: var(--muted); font-weight: 600; }
.bar-row { display: flex; align-items: center; gap: 8px; margin: 3px 0; }
.bar-key { flex: 0 0 46px; color: var(--muted); font-size: 12.5px; text-align: right; }
.bar-track { flex: 1; background: var(--blue-soft); border-radius: 4px; height: 14px; }
.bar-fill { display: block; background: var(--blue); border-radius: 4px; height: 14px; }
.bar-row.peak .bar-fill { background: var(--yellow); }
.bar-val { flex: 0 0 40px; font-size: 12.5px; font-variant-numeric: tabular-nums; }
details {
  background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
  padding: 8px 14px; margin: 6px 0;
}
details > summary { cursor: pointer; font-size: 14px; color: var(--blue); }
details > summary:hover { text-decoration: underline; }
details[open] > summary { margin-bottom: 8px; }
details .scroll { margin-top: 4px; }
.empty {
  background: var(--surface); border: 1px dashed var(--line); border-radius: 8px;
  padding: 14px 16px; color: var(--muted);
}
footer {
  border-top: 1px solid var(--line); margin-top: 34px; padding-top: 14px;
  color: var(--muted); font-size: 12.5px;
}
a:focus-visible, [tabindex]:focus-visible {
  outline: 2px solid var(--blue); outline-offset: 2px;
}
@media print {
  body { background: #fff; padding: 0; font-size: 12px; }
  .card, table, .panel { box-shadow: none; break-inside: avoid; }
  section { break-inside: avoid; }
  tbody tr:hover td { background: transparent; }
}
"""


def _card(label: str, value: Any, tone: str = "", unit: str = "") -> str:
    css = f" {tone}" if tone else ""
    suffix = f' <span class="unit">{esc(unit)}</span>' if unit else ""
    return (
        f'<div class="card{css}"><div class="label">{esc(label)}</div>'
        f'<div class="value">{esc(value)}{suffix}</div></div>'
    )


def _bars(rows: list[tuple[str, int]]) -> str:
    if not rows:
        return '<div class="empty">Нет данных за период.</div>'
    peak = max(value for _, value in rows) or 1
    chunks = []
    for key, value in rows:
        width = round(value / peak * 100)
        peak_css = " peak" if value == peak and value else ""
        chunks.append(
            f'<div class="bar-row{peak_css}"><span class="bar-key">{esc(key)}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{width}%"></span></span>'
            f'<span class="bar-val">{value}</span></div>'
        )
    return "".join(chunks)


def _secs(value: int | None) -> str:
    return fmt_duration(value) if value is not None else "—"


def _table(caption: str, head: list[str], rows: list[str], num_from: int = 1) -> str:
    ths = "".join(
        f'<th scope="col"{" class=num" if i >= num_from else ""}>{esc(h)}</th>'
        for i, h in enumerate(head)
    )
    return (
        f"<div class='scroll'><table><caption>{esc(caption)}</caption>"
        f"<thead><tr>{ths}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _pill(value: int, bad_when_positive: bool = True) -> str:
    if value and bad_when_positive:
        return f'<span class="pill bad">{value}</span>'
    return str(value)


def _page(period_label: str, body: str) -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Аналитика чатов — {esc(period_label)}</title>"
        f"<style>{_CSS}</style></head><body><main>{body}</main></body></html>"
    )


def _quality(summary: dict) -> str:
    if not summary["unresolved"]:
        return "Автор определён у всех исходящих."
    return (
        f"Исходящих без автора: {summary['unresolved']} "
        f"({summary['unresolved_pct']}%) — метрики по этим сообщениям "
        "в разрезе сотрудников не считаются."
    )


def _breach_details(
    people: list[dict], breaches: dict[int, dict], tz: ZoneInfo, calendar_cfg: dict[str, Any]
) -> str:
    """Раскрывающиеся блоки по сотрудникам с просрочками и разбивка «по дням»."""
    from app.services.calendar import CalendarHistory, business_seconds

    # Ожидание — по графику, действовавшему в момент обращения, как и сама просрочка.
    calendar = CalendarHistory(calendar_cfg)

    blocks: list[str] = []
    # Ключ дня — дата, а не строка «дд.мм»: иначе порядок ломается на границе месяца.
    by_day: dict[date | None, int] = {}
    for row in people:
        hit = breaches.get(row["staff_id"])
        if not hit or not hit.get("items"):
            continue
        items = sorted(hit["items"], key=lambda i: i["opened_at"])
        lines = []
        for item in items:
            since, until = item["since"], item["until"]
            day_key = since.astimezone(tz).date() if since else None
            by_day[day_key] = by_day.get(day_key, 0) + 1
            delay = item["business_delay"]
            if delay is None and since and until:
                delay = business_seconds(
                    since, until, calendar.at(item.get("opened_at") or since)
                )
            stage = (
                "реакция" if item["kind"] == "reaction" else "ответ после передачи"
            )
            when_client = since.astimezone(tz).strftime("%d.%m %H:%M") if since else "?"
            when_reply = until.astimezone(tz).strftime("%d.%m %H:%M") if until else "—"
            lines.append(
                f"<tr><td>{esc((item['title'] or '')[:40])}</td><td>{stage}</td>"
                f"<td>{when_client}</td><td>{when_reply}</td>"
                f"<td class='num'>{esc(_secs(delay)) if delay is not None else '—'}</td></tr>"
            )
        blocks.append(
            f"<details><summary>{esc(row['full_name'])} — просрочек: {hit['count']} "
            "(нажмите, чтобы раскрыть)</summary>"
            + _table(
                f"Просрочки: {row['full_name']}",
                ["Чат", "Ступень", "Клиент написал / передано", "Ответили", "Ждали (рабочее)"],
                lines,
                num_from=4,
            )
            + "</details>"
        )
    if not blocks:
        return ""
    # День без времени обращения — в конце.
    day_rows = sorted(by_day.items(), key=lambda kv: (kv[0] is None, kv[0] or date.min))
    days_block = (
        "<details><summary>Просрочки по дням (нажмите, чтобы раскрыть)</summary>"
        "<div class='panel' style='margin-top:8px'>"
        + _bars(
            [(f"{day:%d.%m}" if day else "?", count) for day, count in day_rows]
        )
        + "</div></details>"
    )
    return (
        "<h3>Детализация просрочек</h3><div class='details'>"
        + "".join(blocks)
        + days_block
        + "</div>"
    )


def _staff_rows(people: list[dict], breaches: dict[int, dict]) -> list[str]:
    rows = []
    for row in people:
        hit = breaches.get(row["staff_id"]) or {}
        rows.append(
            f"<tr><td>{esc(row['full_name'])}</td>"
            f"<td class='num'>{row['episodes']}</td>"
            f"<td class='num'>{esc(_secs(row['median']))}</td>"
            f"<td class='num'>{esc(_secs(row['p90']))}</td>"
            f"<td class='num'>{_pill(int(hit.get('reaction') or 0))}</td>"
            f"<td class='num'>{_pill(int(hit.get('specialist') or 0))}</td></tr>"
        )
    return rows


# Просрочки по ступеням — теми же условиями, что карточки «Главного»,
# чтобы суммы по людям сходились с итогом.
_STAFF_HEAD = [
    "Сотрудник",
    "Обращений",
    "Обычно",
    "9 из 10 — быстрее, чем",
    "Просрочек: реакция",
    "Просрочек: ответ",
]


async def build_dashboard_html(
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    period_label: str,
    calendar_cfg: dict[str, Any],
    now: datetime,
) -> str:
    tz = ZoneInfo(calendar_cfg.get("timezone") or "Europe/Moscow")

    summary = await load_summary(session, start, end)
    speed = await load_speed(session, start, end)
    people = await staff_speed(session, start, end)
    chats = await problem_chats(session, start, end)
    profile = await load_profile(
        session, start, end, calendar_cfg.get("timezone") or "Europe/Moscow"
    )
    outside = await after_hours(session, start, end, calendar_cfg)

    total = speed["total"]
    # Ступени — независимые множества со своими знаменателями: реакция — все
    # обращения, требующие ответа; ответ специалиста — только переданные.
    answerable = total - speed["no_response"]
    handoffs = speed["handoffs"]
    on_time_pct = (
        round((answerable - speed["breach_reaction"]) * 100 / answerable)
        if answerable
        else None
    )
    specialist_pct = (
        round((handoffs - speed["breach_substantive"]) * 100 / handoffs) if handoffs else None
    )

    def _tone(pct: int | None) -> str:
        if pct is None:
            return ""
        return "good" if pct >= 90 else ("warn" if pct >= 70 else "bad")

    # `end` — исключающая граница, поэтому последний день периода — секунда до неё.
    dates = (
        f"{start.astimezone(tz):%d.%m} – "
        f"{(end - timedelta(seconds=1)).astimezone(tz):%d.%m.%Y}"
    )

    parts = [
        "<header>",
        "<h1>Аналитика чатов</h1>",
        f"<p class='sub'>Период: {esc(period_label)} ({esc(dates)}) · "
        f"собрано {now.astimezone(tz):%d.%m.%Y %H:%M} "
        f"({esc(calendar_cfg.get('timezone') or 'Europe/Moscow')})</p>",
    ]
    from app.services.calendar import CalendarHistory

    schedule_note = CalendarHistory(calendar_cfg).footnote(start, end)
    if schedule_note:
        parts.append(f"<p class='sub'>🕐 {esc(schedule_note)}</p>")
    parts.append("</header>")

    parts.append("<section><h2>Главное за период</h2><div class='cards'>")
    parts.append(_card("Обращений от клиентов", total))
    parts.append(_card("Сообщений от клиентов", summary["incoming"]))
    parts.append(_card("Сообщений от компании", summary["outgoing"]))
    parts.append(_card("Чатов в анализе", summary["tracked"]))
    parts.append("</div><div class='cards' style='margin-top:10px'>")
    parts.append(
        _card(
            "Первая реакция в срок",
            f"{on_time_pct}" if on_time_pct is not None else "—",
            _tone(on_time_pct),
            "%" if on_time_pct is not None else "",
        )
    )
    parts.append(
        _card(
            "Ответ после передачи в срок",
            f"{specialist_pct}" if specialist_pct is not None else "—",
            _tone(specialist_pct),
            "%" if specialist_pct is not None else "",
        )
    )
    parts.append(
        _card(
            "Закрыто без ответа",
            speed["timed_out"],
            "bad" if speed["timed_out"] else "good",
        )
    )
    parts.append("</div>")
    # Важно: ступень — про сообщение, а не про должность: первым может отреагировать
    # и специалист, поэтому подписи «первая реакция» / «ответ после передачи».
    parts.append(
        f"<h3>Ступень 1 — первая реакция (порог {speed['reaction_limit_min']} мин)</h3>"
        "<div class='cards'>"
        + _card("Обращений, требующих ответа", answerable)
        + _card("Ответили в срок", answerable - speed["breach_reaction"])
        + _card(
            "Просрочено",
            speed["breach_reaction"],
            "bad" if speed["breach_reaction"] else "good",
        )
        + "</div>"
    )
    parts.append(
        "<h3>Ступень 2 — ответ после передачи специалисту "
        f"(срок: {specialist_deadline_label(speed['substantive_limit_min'])})</h3>"
        "<div class='cards'>"
        + _card("Передано специалистам", handoffs)
        + _card("Ответили в срок", handoffs - speed["breach_substantive"])
        + _card(
            "Просрочено",
            speed["breach_substantive"],
            "bad" if speed["breach_substantive"] else "good",
        )
        + "</div>"
    )
    parts.append(
        "<div class='note'>Ступень — про сообщение, а не про должность: первым "
        "отреагировать может и специалист. Одно обращение может быть просрочено "
        "на обеих ступенях. В таблицах сотрудников ниже просрочки разложены по "
        "тем же ступеням — суммы сходятся с этими карточками.</div>"
    )
    parts.append(
        "<div class='legend'>"
        "<p><b>Обращение</b> — вопрос клиента. Несколько сообщений подряд "
        "об одном и том же — это одно обращение, поэтому обращений меньше, "
        "чем сообщений.</p>"
        "<p><b>Реакция</b> — первый ответ компании на обращение, любой "
        f"(даже «добрый день»). Срок — {speed['reaction_limit_min']} минут "
        "рабочего времени.</p>"
        "<p><b>Ответ специалиста</b> — ответ по сути вопроса. Если менеджер "
        "передал вопрос специалисту, срок — "
        f"{specialist_deadline_label(speed['substantive_limit_min'])}. "
        "Просрочки двух ступеней считаются отдельно: у первой реакции "
        "знаменатель — все обращения, требующие ответа, у ответа после "
        "передачи — только переданные. Одно обращение может быть "
        "просрочено на обеих ступенях.</p>"
        "<p><b>Закрыто без ответа</b> — ответа так и не было: бот ждал "
        f"{speed['wait_reaction_hours']} ч после срока реакции "
        f"(или {speed['wait_specialist_days']} дн после срока специалиста) "
        "и снял обращение с ожидания. "
        "Ответят позже — обращение пересчитается как отвеченное, с честным "
        "временем ожидания; останется здесь только то, где клиент к тому "
        "моменту успел написать снова. Конкретные случаи смотрите в боте: "
        "Отчёты → «Остались без ответа».</p>"
        "</div></section>"
    )

    parts.append("<section><h2>Скорость ответа</h2>")
    speed_rows = [
        "<tr><td>Первая реакция</td>"
        f"<td class='num'>{esc(_secs(speed['ttfr_median']))}</td>"
        f"<td class='num'>{esc(_secs(speed['ttfr_p90']))}</td></tr>",
        "<tr><td>Ответ специалиста</td>"
        f"<td class='num'>{esc(_secs(speed['ttfa_median']))}</td>"
        f"<td class='num'>{esc(_secs(speed['ttfa_p90']))}</td></tr>",
    ]
    parts.append(
        _table(
            "Время ответа за период, рабочее время по графику компании",
            ["Показатель", "Типичное время", "9 из 10 — быстрее, чем"],
            speed_rows,
        )
    )
    parts.append(
        "<div class='note'>«Типичное время» — медиана: половина обращений "
        "получила ответ быстрее, половина дольше. Правая колонка отсекает "
        "редкие худшие случаи: дольше неё ждало только одно обращение "
        "из десяти.</div></section>"
    )

    # Специалисты и менеджеры — отдельными таблицами; роль определяется
    # по передачам, неопределённые — третьей группой, а не исключением.
    parts.append("<section><h2>Сотрудники</h2>")
    if not people:
        parts.append("<div class='empty'>За период никто не отвечал на обращения.</div>")
    else:
        from app.services.report_lab import staff_breaches

        breaches = await staff_breaches(session, start, end)
        groups = [
            ("Специалисты", [p for p in people if p["role"] == ROLE_SPECIALIST]),
            ("Менеджеры", [p for p in people if p["role"] == ROLE_MANAGER]),
            (
                "Роль пока не определена",
                [p for p in people if p["role"] not in (ROLE_SPECIALIST, ROLE_MANAGER)],
            ),
        ]
        for title, group in groups:
            if not group:
                continue
            parts.append(f"<h3>{esc(title)}</h3>")
            parts.append(
                _table(
                    f"Скорость первой реакции и просрочки по ступеням: {title.lower()}",
                    _STAFF_HEAD,
                    _staff_rows(group, breaches),
                )
            )
        parts.append(_breach_details(people, breaches, tz, calendar_cfg))
        total_reaction = sum(int(h.get("reaction") or 0) for h in breaches.values())
        total_specialist = sum(int(h.get("specialist") or 0) for h in breaches.values())
        parts.append(
            "<div class='note'>Роль определяется по фактической работе "
            "в чатах: кто передаёт вопросы — менеджер, кто закрывает "
            "переданное — специалист. Итого просрочек по людям: реакция "
            f"{total_reaction}, ответ после передачи {total_specialist}"
            + (
                " — сходится с «Главным за период»."
                if (total_reaction, total_specialist)
                == (speed["breach_reaction"], speed["breach_substantive"])
                else " (разница — просрочки без автора: таблицы приписывают просрочку "
                "тому, кто отреагировал или ответил, а реакции или ответа не было "
                "либо автор не определён)."
            )
            + "</div>"
        )
    parts.append("</section>")

    parts.append("<section><h2>Чаты, где не успевают</h2>")
    if not chats:
        parts.append("<div class='empty'>Ни просрочек, ни зависших обращений.</div>")
    else:
        rows = [
            f"<tr><td>{esc(row['title'])}</td>"
            f"<td class='num'>{row['episodes']}</td>"
            f"<td class='num'>{_pill(row['breached'])}</td>"
            f"<td class='num'>{row['waiting']}</td>"
            f"<td class='num'>{esc(_secs(row['median']))}</td></tr>"
            for row in chats
        ]
        parts.append(
            _table(
                "Чаты с просрочками или ожидающими обращениями",
                ["Чат", "Обращений", "Просрочек", "Ждут", "Обычно"],
                rows,
            )
        )
    parts.append("</section>")

    parts.append("<section><h2>Когда пишут клиенты</h2><div class='split'>")
    parts.append("<div class='panel'><h3>По часам</h3>")
    parts.append(
        _bars([(f"{hour:02d}:00", count) for hour, count in sorted(profile["hours"].items())])
    )
    parts.append("</div><div class='panel'><h3>По дням недели</h3>")
    parts.append(
        _bars([(WEEKDAYS[day - 1], count) for day, count in sorted(profile["weekdays"].items())])
    )
    parts.append("</div></div>")
    parts.append(
        "<div class='note'>Считаются сообщения клиентов, суммой за весь "
        "период (не среднее): столбик «10:00» — сколько сообщений пришло "
        "с 10:00 до 11:00 за все дни периода. Жёлтая полоса — самый "
        "загруженный час и день.</div></section>"
    )

    parts.append("<section><h2>Работа вне графика</h2>")
    if not outside:
        parts.append("<div class='empty'>Вне рабочего времени никто не писал.</div>")
    else:
        rows = [
            f"<tr><td>{esc(row['full_name'])}</td>"
            f"<td class='num'>{row['outside']}</td>"
            f"<td class='num'>{row['total']}</td>"
            f"<td class='num'>{round(row['outside'] / row['total'] * 100) if row['total'] else 0}%</td>"
            "</tr>"
            for row in outside
        ]
        parts.append(
            _table(
                "Сообщения сотрудников вне рабочего графика",
                ["Сотрудник", "Вне графика", "Всего", "Доля"],
                rows,
            )
        )
    parts.append("</section>")

    parts.append(
        f"<footer>{esc(_quality(summary))}<br>Файл собран ботом «Аналитика чатов». "
        "Данные внутри — на момент сборки.</footer>"
    )

    return _page(period_label, "".join(parts))


async def write_dashboard(
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    period_label: str,
    calendar_cfg: dict[str, Any],
    now: datetime,
) -> Path:
    html = await build_dashboard_html(
        session,
        start=start,
        end=end,
        period_label=period_label,
        calendar_cfg=calendar_cfg,
        now=now,
    )
    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    # Уникальное имя: иначе два запроса в одну секунду перезаписали бы файл
    # друг друга, и `unlink` первого удалил бы файл второго до отправки.
    handle, raw_path = tempfile.mkstemp(
        prefix=f"dashboard_{now:%Y%m%d_%H%M%S}_", suffix=".html", dir=_EXPORT_DIR
    )
    os.close(handle)
    path = Path(raw_path)
    path.write_text(html, encoding="utf-8")
    return path
