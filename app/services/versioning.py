"""Версии настроек: сроки считаются по правилам, действовавшим ТОГДА.

Обращения пересобираются каждую минуту, поэтому без версий правка настройки
переписала бы прошлые отчёты. Действующая версия — поля раздела; рядом `since`
(None — «всегда») и `history` (прошлые версии). Модуль ни от чего не зависит.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Сколько версий храним (страховка от роста JSON). Вытесняется самая старая,
# и всё, что было до неё, считается по ней.
MAX_VERSIONS = 50

_BEGINNING = datetime(1, 1, 1, tzinfo=timezone.utc)


def _zone(name: Any) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "Europe/Moscow"))
    except Exception:  # noqa: BLE001 — в базе может лежать неизвестный пояс
        return ZoneInfo("Europe/Moscow")


def parse_since(raw: Any, timezone_name: Any = None) -> datetime | None:
    """Момент вступления версии в силу; None — «действовала всегда до следующей».
    Наивное время трактуется в рабочем поясе, а не в UTC.
    """
    if raw is None or raw == "":
        return None
    moment = raw
    if not isinstance(moment, datetime):
        try:
            moment = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_zone(timezone_name))
    return moment.astimezone(timezone.utc)


def snapshot(values: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    version = {key: values.get(key) for key in fields}
    version["since"] = values.get("since")
    return version


def versions(
    values: dict[str, Any], fields: tuple[str, ...], timezone_name: Any = None
) -> list[dict[str, Any]]:
    """Все версии по возрастанию `since`; последняя — действующая. Недостающие поля
    записи истории берутся из действующей настройки.
    """
    raw = values.get("history")
    result: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        version = {key: entry.get(key, values.get(key)) for key in fields}
        version["since"] = parse_since(entry.get("since"), timezone_name)
        result.append(version)

    current = {key: values.get(key) for key in fields}
    current["since"] = parse_since(values.get("since"), timezone_name)
    result.append(current)

    result.sort(key=lambda item: item["since"] or _BEGINNING)
    return result


def push_version(
    values: dict[str, Any], fields: tuple[str, ...], moment: datetime
) -> None:
    """Сдвинуть действующую версию в историю. Звать ПЕРЕД записью нового значения."""
    history = [
        entry for entry in (values.get("history") or []) if isinstance(entry, dict)
    ]
    history.append(snapshot(values, fields))
    values["history"] = history[-MAX_VERSIONS:]
    values["since"] = moment.isoformat()


class History:
    """Значения настроек на любой момент прошлого. Заводится один на проход:
    версии разбираются один раз.
    """

    __slots__ = ("versions", "fields")

    def __init__(
        self,
        values: dict[str, Any],
        fields: tuple[str, ...],
        timezone_name: Any = None,
    ) -> None:
        self.fields = fields
        self.versions = versions(values, fields, timezone_name)

    @property
    def current(self) -> dict[str, Any]:
        return self.versions[-1]

    def at(self, moment: datetime | None) -> dict[str, Any]:
        """Значения, действовавшие в момент `moment`. До самой ранней версии — она же."""
        if moment is None:
            return self.current
        chosen = self.versions[0]
        for version in self.versions:
            since = version["since"]
            if since is not None and since > moment:
                break
            chosen = version
        return chosen

    def changed(self) -> bool:
        return len(self.versions) > 1

    def spans(
        self, start: datetime, end: datetime
    ) -> list[tuple[dict[str, Any], datetime | None]]:
        """Версии, действовавшие внутри [start, end): (версия, с какого момента)."""
        result: list[tuple[dict[str, Any], datetime | None]] = []
        for index, version in enumerate(self.versions):
            since = version["since"]
            until = (
                self.versions[index + 1]["since"]
                if index + 1 < len(self.versions)
                else None
            )
            if since is not None and since >= end:
                continue
            if until is not None and until <= start:
                continue
            result.append((version, since))
        return result
