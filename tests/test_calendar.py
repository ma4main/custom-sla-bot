"""Рабочий календарь и срок ответа.

Здесь живёт правило, на котором держатся все алерты: порог не переносится
через нерабочее время по частям, новый день даёт его целиком заново.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.calendar import business_seconds, response_deadline

DAY = {
    "weekdays": [1, 2, 3, 4, 5],
    "start": "10:00",
    "end": "19:00",
    "timezone": "UTC",
    "holidays": [],
}
HALF_HOUR = 30 * 60


def moment(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)


def test_business_seconds_skips_nonworking_time():
    # Пятница 19:00 → понедельник 09:05 при графике 9–18 = 5 рабочих минут.
    cfg = dict(DAY, start="09:00", end="18:00")
    got = business_seconds(moment(14, 19), moment(17, 9, 5), cfg)
    assert got == 300


def test_business_seconds_zero_for_reversed_range():
    assert business_seconds(moment(24, 15), moment(24, 12), DAY) == 0


@pytest.mark.parametrize(
    "start, expected",
    [
        (moment(24, 14), moment(24, 14, 30)),          # середина дня
        (moment(24, 18, 30), moment(24, 19)),          # ровно впритык к закрытию
        (moment(24, 18, 40), moment(25, 10, 30)),      # не помещается — завтра заново
        (moment(24, 23), moment(25, 10, 30)),          # ночь
        (moment(22, 12), moment(24, 10, 30)),          # выходной
        (moment(21, 18, 50), moment(24, 10, 30)),      # вечер пятницы
    ],
)
def test_response_deadline(start, expected):
    assert response_deadline(start, HALF_HOUR, DAY) == expected


def test_deadline_skips_holiday():
    cfg = dict(DAY, holidays=["2026-08-25"])
    assert response_deadline(moment(24, 18, 40), HALF_HOUR, cfg) == moment(26, 10, 30)


def test_deadline_longer_than_working_day_does_not_slip_forever():
    got = response_deadline(moment(24, 14), 20 * 3600, DAY)
    assert got is not None and got.date() == date(2026, 8, 25)


def test_empty_weekdays_falls_back_to_workweek():
    # Недосохранённая настройка не должна означать «работаем никогда»:
    # поведение то же, что у business_seconds.
    got = response_deadline(moment(24, 14), HALF_HOUR, dict(DAY, weekdays=[]))
    assert got == moment(24, 14, 30)


def test_no_working_day_at_all_returns_none():
    holidays = [(date(2026, 8, 24) + timedelta(days=i)).isoformat() for i in range(400)]
    assert response_deadline(moment(24, 14), HALF_HOUR, dict(DAY, holidays=holidays)) is None
