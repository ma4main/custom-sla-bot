"""«Провайдер отвечает медленно»: вход за два тика подряд, выход после десяти
минут спокойствия, одно уведомление на вход и на выход, молчание при `down`,
в текстах — только счётчики и названия чатов.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.ai_health import (
    INITIAL,
    LATENCY_WINDOW,
    OUTCOME_SUCCESS,
    SLOW_CLEAR_MINUTES,
    SLOW_ENTRY_TICKS,
    SLOW_MIN_SAMPLES,
    SLOW_REMINDER_HOURS,
    STATUS_DOWN,
    latency_median_ms,
    latency_text,
    next_speed_state,
    next_state,
    record_speed,
    slow_cleared_message,
    slow_message,
    slow_still_message,
)
from tests.conftest import requires_db

NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)

# Пороги по умолчанию (`AI_SLOW_LATENCY_SECONDS`, `AI_SLOW_QUEUE_MINUTES`).
LATENCY_LIMIT = 10
QUEUE_LIMIT = 15

# Ровно минимум замеров: медиана без него не считается принципиально.
SLOW_CALLS = [14_000] * SLOW_MIN_SAMPLES
# Целое окно быстрых вызовов — столько нужно, чтобы медиана действительно
# опустилась: наполовину медленное окно медленным и остаётся, и это
# правильно (провайдер ещё не выправился).
FAST_CALLS = [1_000] * LATENCY_WINDOW


def _tick(state, *, latencies=(), pending=0, chats=0, oldest=None, now=NOW, suppressed=False):
    return next_speed_state(
        state,
        latencies_ms=latencies,
        pending=pending,
        chats=chats,
        oldest_minutes=oldest,
        now=now,
        latency_threshold_seconds=LATENCY_LIMIT,
        queue_threshold_minutes=QUEUE_LIMIT,
        suppressed=suppressed,
    )


# ── 1. Вход: два тика подряд, не один ─────────────────────────────────


def test_one_slow_tick_is_not_enough():
    """Одиночный всплеск — не повод будить людей."""
    state, transition = _tick(None, latencies=SLOW_CALLS)

    assert transition is None
    assert state["slow"] is False
    assert state["slow_streak"] == 1


def test_two_slow_ticks_in_a_row_enter_the_state_once():
    state, _ = _tick(None, latencies=SLOW_CALLS)
    state, transition = _tick(state, latencies=[14_000], now=NOW + timedelta(minutes=1))

    assert transition == "slow"
    assert state["slow"] is True
    assert state["slow_since"] == (NOW + timedelta(minutes=1)).isoformat()
    assert SLOW_ENTRY_TICKS == 2


def test_the_entry_notice_does_not_repeat_every_tick():
    """Гистерезис: состояние уже объявлено, повторять его нечего."""
    state, _ = _tick(None, latencies=SLOW_CALLS)
    state, _ = _tick(state, latencies=[14_000], now=NOW + timedelta(minutes=1))

    for minute in range(2, 12):
        state, transition = _tick(
            state, latencies=[14_000], now=NOW + timedelta(minutes=minute)
        )
        assert transition is None, f"повтор уведомления на {minute}-й минуте"


def test_a_single_fast_tick_breaks_the_streak():
    state, _ = _tick(None, latencies=SLOW_CALLS)
    state, _ = _tick(state, latencies=FAST_CALLS, now=NOW + timedelta(minutes=1))

    assert state["slow_streak"] == 0
    assert state["slow"] is False


def test_a_short_window_never_enters_by_latency():
    """Меньше десяти вызовов — судить не о чем, даже если все медленные."""
    state = None
    for tick in range(4):
        state, transition = _tick(
            state,
            latencies=[30_000, 30_000],
            now=NOW + timedelta(minutes=tick),
        )
        assert transition is None
    assert len(state["latencies"]) < SLOW_MIN_SAMPLES


# ── 2. Вход по отставанию очереди — вторая, независимая причина ────────


def test_an_old_queue_enters_the_state_without_any_latency_data():
    """Разметка может отставать и при быстрых вызовах: их просто мало.

    По чатам, стоящим за отказавшим сообщением, вызовов не делается вовсе,
    а очередь растёт.
    """
    state, first = _tick(None, pending=49, chats=5, oldest=78)
    state, second = _tick(
        state, pending=49, chats=5, oldest=79, now=NOW + timedelta(minutes=1)
    )

    assert first is None
    assert second == "slow"
    assert state["latencies"] == [], "состояние вошло по очереди, а не по задержке"


def test_a_queue_under_the_threshold_is_normal_work():
    state, _ = _tick(None, pending=20, chats=3, oldest=QUEUE_LIMIT - 1)
    state, transition = _tick(
        state, pending=20, chats=3, oldest=QUEUE_LIMIT - 1, now=NOW + timedelta(minutes=1)
    )

    assert transition is None
    assert state["slow"] is False


def test_the_queue_snapshot_is_kept_for_the_health_screen():
    state, _ = _tick(None, pending=49, chats=5, oldest=78)

    assert state["queue"]["pending"] == 49
    assert state["queue"]["chats"] == 5
    assert state["queue"]["oldest_minutes"] == 78


# ── 3. Выход: медиана ниже ПОЛОВИНЫ порога, и десять минут подряд ──────


def _slow_state(now=NOW):
    state, _ = _tick(None, latencies=SLOW_CALLS, now=now)
    state, transition = _tick(
        state, latencies=[14_000], now=now + timedelta(minutes=1)
    )
    assert transition == "slow"
    return state


def test_the_state_does_not_clear_on_the_first_calm_tick():
    state = _slow_state()
    state, transition = _tick(
        state, latencies=FAST_CALLS, now=NOW + timedelta(minutes=2)
    )

    assert transition is None
    assert state["slow"] is True
    assert state["slow_ok_since"] == (NOW + timedelta(minutes=2)).isoformat()


def test_the_state_clears_after_ten_calm_minutes():
    state = _slow_state()
    calm_from = NOW + timedelta(minutes=2)
    state, _ = _tick(state, latencies=FAST_CALLS, now=calm_from)
    state, transition = _tick(
        state,
        latencies=[1_000],
        now=calm_from + timedelta(minutes=SLOW_CLEAR_MINUTES),
    )

    assert transition == "slow_cleared"
    assert state["slow"] is False
    assert state["slow_since"] is None
    assert state["slow_notified_at"] is None


def test_a_median_between_half_and_full_threshold_keeps_the_state():
    """Порог выхода ниже порога входа — иначе пошли бы пары сообщений."""
    state = _slow_state()
    middle = [LATENCY_LIMIT * 1000 * 3 // 4] * SLOW_MIN_SAMPLES
    calm_from = NOW + timedelta(minutes=2)
    state, _ = _tick(state, latencies=middle, now=calm_from)
    state, transition = _tick(
        state, latencies=middle, now=calm_from + timedelta(minutes=SLOW_CLEAR_MINUTES)
    )

    assert transition is None
    assert state["slow"] is True


def test_a_late_queue_alone_keeps_the_state_even_on_fast_calls():
    state = _slow_state()
    calm_from = NOW + timedelta(minutes=2)
    state, _ = _tick(state, latencies=FAST_CALLS, now=calm_from, oldest=QUEUE_LIMIT + 1)
    state, transition = _tick(
        state,
        latencies=FAST_CALLS,
        now=calm_from + timedelta(minutes=SLOW_CLEAR_MINUTES),
        oldest=QUEUE_LIMIT + 1,
    )

    assert transition is None
    assert state["slow"] is True


def test_a_quiet_window_stops_counting_after_ten_minutes():
    """Очередь разобрана, вызовов нет — старые замеры больше не судья.

    Без этого состояние «медленно», начавшееся на последних вызовах перед
    затишьем, не вышло бы никогда: новых замеров не приходит, медиана
    навсегда осталась бы высокой.
    """
    state = _slow_state()
    quiet_from = NOW + timedelta(minutes=2)
    state, still = _tick(state, now=quiet_from)
    assert still is None, "минуту назад замеры были — окно ещё судья"

    # Окно протухло: спокойствие только с этого момента, и ему ещё
    # предстоит отстоять свои десять минут.
    state, waiting = _tick(state, now=quiet_from + timedelta(minutes=SLOW_CLEAR_MINUTES))
    assert waiting is None

    state, transition = _tick(
        state, now=quiet_from + timedelta(minutes=2 * SLOW_CLEAR_MINUTES)
    )
    assert transition == "slow_cleared"


# ── 4. «Всё ещё медленно» — не чаще раза в три часа ────────────────────


def test_the_reminder_waits_three_hours():
    state = _slow_state()
    moment = NOW + timedelta(minutes=1)

    state, early = _tick(
        state,
        latencies=[14_000],
        now=moment + timedelta(hours=SLOW_REMINDER_HOURS) - timedelta(minutes=1),
    )
    assert early is None

    state, due = _tick(
        state, latencies=[14_000], now=moment + timedelta(hours=SLOW_REMINDER_HOURS)
    )
    assert due == "slow_still"

    state, again = _tick(
        state,
        latencies=[14_000],
        now=moment + timedelta(hours=SLOW_REMINDER_HOURS, minutes=1),
    )
    assert again is None, "напоминание пошло на каждом тике"


# ── 5. При сбое провайдера про скорость не сообщают ────────────────────


def test_nothing_is_said_while_the_provider_is_down():
    """Про лежащего провайдера уже ушло своё сообщение."""
    state, _ = _tick(None, latencies=SLOW_CALLS, suppressed=True)
    state, transition = _tick(
        state, latencies=[14_000], now=NOW + timedelta(minutes=1), suppressed=True
    )

    assert transition is None
    assert state["slow"] is True, "состояние всё же ведётся — молчит только сообщение"
    assert state["slow_notified_at"] is None


def test_a_silent_entry_means_a_silent_exit():
    """«Снова нормально» без «медленно» человек прочитать не должен."""
    state, _ = _tick(None, latencies=SLOW_CALLS, suppressed=True)
    state, _ = _tick(
        state, latencies=[14_000], now=NOW + timedelta(minutes=1), suppressed=True
    )
    calm_from = NOW + timedelta(minutes=2)
    state, _ = _tick(state, latencies=FAST_CALLS, now=calm_from)
    state, transition = _tick(
        state,
        latencies=[1_000],
        now=calm_from + timedelta(minutes=SLOW_CLEAR_MINUTES),
    )

    assert transition is None
    assert state["slow"] is False


@requires_db
async def test_record_speed_stays_silent_on_a_down_provider(session):
    """Тот же запрет, но через реальное состояние в базе."""
    from app.services.ai_health import save_state

    await save_state(session, dict(INITIAL, status=STATUS_DOWN))
    for tick in range(SLOW_ENTRY_TICKS):
        _, transition, _ = await record_speed(
            session,
            latencies_ms=SLOW_CALLS,
            pending=49,
            chats=5,
            oldest_minutes=78,
            latency_threshold_seconds=LATENCY_LIMIT,
            queue_threshold_minutes=QUEUE_LIMIT,
            now=NOW + timedelta(minutes=tick),
        )
        assert transition is None


@requires_db
async def test_record_speed_reports_the_entry_and_keeps_the_window(session):
    for tick in range(SLOW_ENTRY_TICKS):
        state, transition, _ = await record_speed(
            session,
            latencies_ms=SLOW_CALLS,
            pending=49,
            chats=5,
            oldest_minutes=78,
            latency_threshold_seconds=LATENCY_LIMIT,
            queue_threshold_minutes=QUEUE_LIMIT,
            now=NOW + timedelta(minutes=tick),
        )
    assert transition == "slow"
    assert latency_median_ms(state) == 14_000
    assert len(state["latencies"]) == LATENCY_WINDOW


# ── 6. Окно задержек переживает удачный проход ─────────────────────────


def test_a_successful_pass_does_not_erase_the_latency_window():
    """Регрессия: ветка успеха собирает состояние из `INITIAL` заново.

    Без явного переноса ключей скорости первый же удачный проход стирал
    бы и окно замеров, и незакрытое «медленно» — то есть ровно то, ради
    чего состояние заведено: провайдер ОТВЕЧАЕТ, просто медленно.
    """
    slow = _slow_state()
    after = next_state(slow, OUTCOME_SUCCESS, None, NOW + timedelta(minutes=2))

    assert after["slow"] is True
    assert after["latencies"] == slow["latencies"]
    assert after["slow_since"] == slow["slow_since"]
    assert after["slow_notified_at"] == slow["slow_notified_at"]
    # И при этом прежнее поведение не тронуто.
    assert after["status"] == "ok"
    assert after["consecutive_failures"] == 0


def test_the_window_keeps_only_the_last_calls():
    state, _ = _tick(None, latencies=list(range(1, 60)))

    assert len(state["latencies"]) == LATENCY_WINDOW
    assert state["latencies"][-1] == 59


# ── 7. Тексты: цифры, простой язык и ни слова из переписки ─────────────


def test_the_entry_text_answers_the_four_questions():
    text = slow_message(median_ms=14_000, pending=49, chats=5, oldest_minutes=78)

    assert "Провайдер ИИ отвечает медленнее обычного" in text
    assert "14 с" in text and "около 1 секунды" in text
    assert "49 сообщений в 5 чатах" in text
    assert "самое старое ждёт 78 минут" in text
    assert "алерты о просрочке" in text
    assert "5 и 15 минут" in text and "резервной модели" in text
    assert "Состояние системы" in text


def test_the_texts_carry_no_message_content():
    """Утечка переписки в уведомление о состоянии — отдельный инцидент.

    Сборщики принимают ТОЛЬКО числа и названия чатов: текста сообщения
    им передать физически нечем.
    """
    import inspect

    for builder in (slow_message, slow_still_message, slow_cleared_message):
        parameters = set(inspect.signature(builder).parameters) - {"self"}
        assert not parameters & {"text", "payload", "message", "messages"}

    text = slow_still_message(
        median_ms=14_000,
        pending=49,
        chats=5,
        oldest_minutes=78,
        since_seconds=4_320,
        skipped=2,
        skipped_chats=["Ромашка"],
    )
    assert "ПП в банке" not in text
    assert "«Ромашка»" in text


def test_chat_titles_are_escaped_for_html():
    """Название чата задают участники: «<» в нём ломает разбор целиком."""
    text = slow_cleared_message(
        median_ms=1_000,
        since_seconds=3_600,
        skipped=1,
        skipped_chats=["ООО <Ромашка> & Ко"],
    )

    assert "&lt;Ромашка&gt;" in text and "&amp;" in text
    assert "<Ромашка>" not in text


def test_the_still_slow_text_lists_what_was_skipped():
    text = slow_still_message(
        median_ms=21_000,
        pending=60,
        chats=6,
        oldest_minutes=95,
        since_seconds=3 * 3600,
        skipped=3,
        skipped_chats=["Ромашка", "Вектор"],
    )

    assert "всё ещё отвечает медленно" in text
    assert "3 часа" in text
    assert "Без разметки осталось 3 сообщения" in text
    assert "«Ромашка», «Вектор»" in text
    assert "просмотреть глазами" in text


def test_the_exit_text_says_how_long_it_lasted():
    text = slow_cleared_message(median_ms=900, since_seconds=4_320, skipped=0)

    assert "снова отвечает нормально, очередь разобрана" in text
    assert "1 час 12 мин" in text
    assert "Без разметки" not in text, "пропусков не было — и говорить не о чем"


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "1 сообщение"), (2, "2 сообщения"), (5, "5 сообщений"), (11, "11 сообщений")],
)
def test_counts_are_written_in_russian(count, expected):
    """«Ждут разметки 1 сообщений» читается как поломка, а не как цифра."""
    text = slow_message(median_ms=14_000, pending=count, chats=1, oldest_minutes=20)
    assert expected in text


def test_the_health_screen_latency_line_shows_the_window():
    state = dict(INITIAL, latencies=[1_000, 1_200, 30_000])

    line = latency_text(state)

    assert "медиана по 3 вызовам" in line
    assert "1,2 с" in line
    assert "максимум 30 с" in line
    assert latency_text(dict(INITIAL)) == "замеров пока нет"


# ── 8. Воркер: замер задержки и отставание очереди ────────────────────


def test_the_worker_measures_every_network_call():
    """И успешный, и отказавший: таймаут — самый медленный ответ."""
    import inspect

    from app.worker import main as worker

    source = inspect.getsource(worker.classify_pending)
    assert source.count("latencies_ms.append") == 2, (
        "замеряется не каждый вызов — окно будет мерить удачные дни"
    )
    assert "latency_ms_median" in source and "latency_ms_max" in source


def test_the_tick_log_does_not_carry_the_raw_window():
    import inspect

    from app.worker import main as worker

    source = inspect.getsource(worker.run_once)
    assert 'key != "latencies_ms"' in source


@requires_db
async def test_the_backlog_counts_chats_blocked_behind_a_failure(session):
    """Причинного фильтра в замере отставания НЕТ — и это главное.

    С ним очередь выглядела бы короче правды в несколько раз: сообщения
    чатов, стоящих за отказавшим, он прячет — ровно те, из-за которых
    отставание и считают.
    """
    from datetime import datetime as _dt

    from sqlalchemy import select

    from app.db.models import BusinessSide, Message, TransportActorKind
    from app.services.ai_stats import pending_backlog
    from app.worker.main import causal_pending_condition
    from tests.test_queue_retry_backoff import ACCEPTED, _chat_with_message, _failure

    now = _dt.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9100, message_id=91000, sent_at=now - timedelta(minutes=80)
    )
    session.add(
        Message(
            id=91001,
            chat_id=9100,
            tg_message_id=91001,
            sent_at=now - timedelta(minutes=40),
            text="а что со счётом?",
            business_side=BusinessSide.CLIENT,
            transport_actor_kind=TransportActorKind.HUMAN_USER,
        )
    )
    session.add(_failure(91000, now - timedelta(minutes=30)))
    await session.flush()

    # Причинный фильтр прячет сообщение, стоящее за отказавшим.
    visible_to_the_worker = list(
        await session.scalars(
            select(Message.id)
            .where(Message.chat_id == 9100)
            .where(causal_pending_condition(ACCEPTED))
        )
    )
    assert 91001 not in visible_to_the_worker

    # Замер отставания считает ОБА: именно столько сообщений ждёт разметки.
    pending, chats, oldest = await pending_backlog(session, ACCEPTED, now=now)

    assert (pending, chats) == (2, 1)
    assert oldest is not None
    assert int((now - oldest).total_seconds() // 60) == 80


@requires_db
async def test_the_health_screen_shows_latency_queue_and_skips(session, monkeypatch):
    """Экран состояния обязан отвечать на «почему разметка отстаёт»."""
    from contextlib import asynccontextmanager
    from datetime import datetime as _dt

    import app.bot.handlers.health as health
    from app.db.models import BotRole, BotUser, BotUserState
    from app.services.ai_health import save_state
    from tests.test_queue_retry_backoff import (
        PRIMARY,
        _chat_with_message,
        _failure,
        _technical,
    )

    class _Message:
        def __init__(self):
            self.text = None
            self.markup = None

        async def edit_text(self, text, reply_markup=None, parse_mode=None, **kwargs):
            self.text = text
            self.markup = reply_markup

    class _Query:
        def __init__(self):
            self.message = _Message()

        async def answer(self, *args, **kwargs):
            return None

    now = _dt.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9102, message_id=91003, sent_at=now - timedelta(minutes=78)
    )
    session.add(_failure(91003, now - timedelta(minutes=30)))
    await _chat_with_message(
        session, chat_id=9103, message_id=91004, sent_at=now - timedelta(minutes=20)
    )
    session.add(_technical(91004))
    await save_state(
        session,
        dict(
            INITIAL,
            latencies=[14_000] * LATENCY_WINDOW,
            latencies_at=now.isoformat(),
            slow=True,
            slow_since=(now - timedelta(minutes=40)).isoformat(),
        ),
    )
    await session.flush()

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(health, "session_scope", fake_scope)
    # `ai_accepted_models` — свойство, а не поле: подменяется то, из чего
    # оно считается (та же модель, под которой записаны строки тестов).
    monkeypatch.setattr(health.get_settings(), "ai_enabled", True, raising=False)
    monkeypatch.setattr(health.get_settings(), "ai_model", PRIMARY, raising=False)
    monkeypatch.setattr(health.get_settings(), "ai_previous_models", "", raising=False)
    monkeypatch.setattr(health.get_settings(), "ai_fallback_model", "", raising=False)

    query = _Query()
    await health.on_health(
        query,
        BotUser(
            tg_user_id=770901,
            role=BotRole.OWNER,
            permissions={},
            state=BotUserState.ACTIVE,
        ),
    )
    text = query.message.text

    assert "Ответ провайдера: 14 с (медиана по 20 вызовам" in text
    assert "Очередь разметки: 1 сообщ. в 1 чат., самое старое ждёт 1 ч 18 мин" in text
    assert "Без разметки за сутки: 1\n" in text
    # И то же самое — предупреждениями, а не только строками статуса.
    assert "Провайдер ИИ отвечает медленнее обычного" in text
    assert "Без разметки за сутки: 1.</b>" in text
    assert "«т»" in text, "чат для ручной проверки не назван"


@requires_db
async def test_skipped_messages_are_countable_by_chat(session):
    """Пропуск обязан быть виден человеку, а не только в `docker logs`."""
    from datetime import datetime as _dt

    from app.services.ai_stats import technical_verdicts
    from tests.test_queue_retry_backoff import _chat_with_message, _technical

    now = _dt.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9101, message_id=91002, sent_at=now - timedelta(hours=2)
    )
    session.add(_technical(91002))
    await session.flush()

    total, titles = await technical_verdicts(session, now - timedelta(days=1))

    assert total == 1
    assert titles == ["т"]
    assert await technical_verdicts(session, now + timedelta(minutes=1)) == (0, [])
