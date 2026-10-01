"""Состояние ИИ-провайдера: когда глушить алерты."""

from datetime import datetime, timezone

from app.services.ai_health import (
    FAILURE_THRESHOLD,
    INITIAL,
    OUTCOME_BUDGET,
    OUTCOME_FAILURE,
    OUTCOME_IDLE,
    OUTCOME_SUCCESS,
    STATUS_DOWN,
    STATUS_OK,
    next_state,
)

NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


def test_idle_does_not_change_anything():
    assert next_state(None, OUTCOME_IDLE, None, NOW) == dict(INITIAL)


def test_single_failure_does_not_silence_alerts():
    state = next_state(dict(INITIAL), OUTCOME_FAILURE, "timeout", NOW)
    assert state["status"] == STATUS_OK
    assert state["consecutive_failures"] == 1


def test_provider_is_down_after_threshold():
    state = dict(INITIAL)
    for _ in range(FAILURE_THRESHOLD):
        state = next_state(state, OUTCOME_FAILURE, "timeout", NOW)
    assert state["status"] == STATUS_DOWN
    assert state["since"] == NOW.isoformat()


def test_success_restores_and_resets_counter():
    state = dict(INITIAL, status=STATUS_DOWN, consecutive_failures=FAILURE_THRESHOLD)
    state = next_state(state, OUTCOME_SUCCESS, None, NOW)
    assert state["status"] == STATUS_OK
    assert state["consecutive_failures"] == 0


def test_budget_limit_goes_down_immediately():
    state = next_state(dict(INITIAL), OUTCOME_BUDGET, "лимит", NOW)
    assert state["status"] == STATUS_DOWN
    assert "лимит" in state["reason"]


# ── Частичный сбой ───────────────────────────────────────────────────────
from app.services.ai_health import (  # noqa: E402
    DEGRADED_MIN_ATTEMPTS,
    DEGRADED_WINDOW,
    failure_share,
)


def _passes(state, pairs):
    for attempted, failed in pairs:
        outcome = OUTCOME_FAILURE if failed >= attempted else OUTCOME_SUCCESS
        state = next_state(state, outcome, "boom" if failed else None, NOW, attempted, failed)
    return state


def test_half_of_requests_failing_is_degraded_but_not_down():
    """Каждый второй запрос падает: статус ok (вердикты идут), деградация — да."""
    state = _passes(dict(INITIAL), [(2, 1)] * 4)  # 8 попыток, 4 неудачи
    assert state["status"] == STATUS_OK, "алерты глушить нельзя — вердикты приходят"
    assert state["degraded"] is True
    assert failure_share(state) == 50


def test_too_few_attempts_do_not_judge():
    state = _passes(dict(INITIAL), [(1, 1), (1, 1)])  # 2 попытки — мало
    assert state["degraded"] is False
    assert failure_share(state) is None
    assert DEGRADED_MIN_ATTEMPTS > 2


def test_window_forgets_old_failures():
    """Провайдер починился: окно вытесняет старые неудачи, деградация снимается."""
    bad = _passes(dict(INITIAL), [(2, 2)] * 3)  # 6 неудач подряд → и DOWN
    assert bad["status"] == STATUS_DOWN
    good = _passes(bad, [(3, 0)] * DEGRADED_WINDOW)
    assert good["status"] == STATUS_OK
    assert good["degraded"] is False
    assert len(good["recent"]) == DEGRADED_WINDOW


def test_idle_and_no_attempts_leave_window_alone():
    state = _passes(dict(INITIAL), [(2, 1)] * 3)
    same = next_state(state, OUTCOME_IDLE, None, NOW)
    assert same["recent"] == state["recent"]
