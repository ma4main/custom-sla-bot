"""Сторож живости: протухла отметка — процесс выходит, дальше работает
restart-policy Docker. Пропустить зависшего — он молчит вечно, убить живого —
цикл перезапусков; решение проверяется как чистая функция."""

from app.health import MAX_AGE_SECONDS, watchdog_reason

BOT_LIMIT = MAX_AGE_SECONDS["bot"]


def test_fresh_heartbeat_is_left_alone():
    assert watchdog_reason(age=30, uptime=10_000, limit=BOT_LIMIT) is None


def test_stale_heartbeat_kills_process():
    reason = watchdog_reason(age=BOT_LIMIT + 1, uptime=10_000, limit=BOT_LIMIT)
    assert reason is not None and "последний круг" in reason


def test_missing_heartbeat_is_tolerated_while_starting_up():
    """Медленный запуск не должен убивать сам себя и уходить в цикл."""
    assert watchdog_reason(age=None, uptime=BOT_LIMIT - 1, limit=BOT_LIMIT) is None


def test_missing_heartbeat_kills_after_grace_period():
    reason = watchdog_reason(age=None, uptime=BOT_LIMIT + 1, limit=BOT_LIMIT)
    assert reason is not None and "ни одного круга" in reason


def test_limit_boundary_is_not_a_kill():
    """Ровно предел — ещё жив: перезапускать работающий хуже, чем подождать."""
    assert watchdog_reason(age=BOT_LIMIT, uptime=10_000, limit=BOT_LIMIT) is None
