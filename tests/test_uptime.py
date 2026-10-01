"""Бот докладывает о простое постфактум.

Порог 5 минут: плановый перезапуск деплоем (полминуты) не считается,
первый запуск без пульса — тоже. Разрыв больше порога — границы простоя
и человеческое сообщение.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.uptime import (
    GAP_THRESHOLD,
    detect_gap,
    downtime_message,
    record_beat,
)
from tests.conftest import requires_db

NOW = datetime(2026, 9, 2, 11, 47, tzinfo=timezone.utc)


@requires_db
async def test_first_start_is_not_downtime(session):
    assert await detect_gap(session, NOW) is None


@requires_db
async def test_short_restart_is_silent_long_gap_is_reported(session):
    await record_beat(session, NOW - timedelta(seconds=40))
    await session.flush()
    assert await detect_gap(session, NOW) is None, "перезапуск деплоем — не простой"

    await record_beat(session, NOW - GAP_THRESHOLD - timedelta(minutes=40))
    await session.flush()
    gap = await detect_gap(session, NOW)
    assert gap is not None
    since, until = gap
    assert until == NOW
    assert (until - since) == GAP_THRESHOLD + timedelta(minutes=40)


def test_downtime_message_reads_like_a_human():
    tz = ZoneInfo("Europe/Moscow")
    since = datetime(2026, 9, 2, 11, 2, tzinfo=timezone.utc)   # 14:02 МСК
    until = datetime(2026, 9, 2, 11, 47, tzinfo=timezone.utc)  # 14:47 МСК
    text = downtime_message(since, until, tz)
    assert "не работал 45 мин" in text
    assert "02.09 14:02 – 14:47" in text
    assert "догружены" in text
