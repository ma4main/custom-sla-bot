"""Здоровье ИИ-провайдера и приостановка алертов при сбое.

Без классификации бот не отличает «спасибо» от вопроса, поэтому при сбое
провайдера алерты приостанавливаются; владельцу уходит одно сообщение о сбое
и одно о восстановлении. Состояние — в setting под служебным ключом `ai_health`
(не в DEFAULTS, в «Настройках» не показывается).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting
from app.text import esc

log = structlog.get_logger(__name__)

STATE_KEY = "ai_health"

STATUS_OK = "ok"
STATUS_DOWN = "down"

# Три неудачных прохода воркера подряд (≈3 минуты) — сбой; единичная ошибка — нет.
FAILURE_THRESHOLD = 3

OUTCOME_SUCCESS = "success"  # хотя бы один вердикт получен
OUTCOME_FAILURE = "failure"  # звали провайдера, все попытки упали
OUTCOME_BUDGET = "budget"    # месячный потолок токенов исчерпан
OUTCOME_IDLE = "idle"        # классифицировать было нечего

# Частичный сбой: доля неудач в окне последних проходов выше порога —
# «деградация». Только предупреждение на экране состояния, алерты не глушатся.
DEGRADED_WINDOW = 10
DEGRADED_MIN_ATTEMPTS = 5
DEGRADED_RATIO = 0.5

# Проба провайдера (бесплатный GET /models), если полчаса не было успешного
# контакта: иначе сбой в простое обнаружился бы только с первым сообщением клиента.
PROBE_AFTER_MINUTES = 30

# Медленный провайдер — отдельное состояние, независимое от `down`: статус «ok»,
# но разметка и алерты отстают. Окно задержек включает и отказавшие вызовы:
# отказ по таймауту — самый медленный вызов.
LATENCY_WINDOW = 20
# Минимум вызовов, чтобы судить о медиане.
SLOW_MIN_SAMPLES = 10
# Вход — два тика подряд: одиночный всплеск не повод будить людей.
SLOW_ENTRY_TICKS = 2
# Выход — условие спокойствия держится 10 минут, чтобы не было пар «медленно / нормально».
SLOW_CLEAR_MINUTES = 10
SLOW_REMINDER_HOURS = 3
# Окно задержек «протухает» без новых вызовов, иначе состояние «медленно»,
# начавшееся перед затишьем, не вышло бы никогда.
LATENCY_FRESH_MINUTES = SLOW_CLEAR_MINUTES

# Ключи скорости переносятся явно: ветка успеха в `next_state` собирает
# состояние из `INITIAL` заново и иначе стёрла бы окно задержек и «медленно».
SPEED_KEYS = (
    "latencies",
    "latencies_at",
    "slow",
    "slow_since",
    "slow_streak",
    "slow_ok_since",
    "slow_notified_at",
    "queue",
)

INITIAL: dict[str, Any] = {
    "status": STATUS_OK,
    "reason": None,
    "consecutive_failures": 0,
    "since": None,
    # [[попыток, неудач], …] по последним проходам, где звали провайдера.
    "recent": [],
    "degraded": False,
    # Последний успешный контакт с провайдером (вердикт или проба).
    "last_contact": None,
    # Резервная модель, если классификация идёт на ней (None — основная);
    # статус при этом «ok», а на экране состояния — «работает на резерве».
    "fallback": None,
    # Длительности последних вызовов классификации, мс.
    "latencies": [],
    "latencies_at": None,
    "slow": False,
    "slow_since": None,
    "slow_streak": 0,
    "slow_ok_since": None,
    # Когда в последний раз сказали «медленно»; None — «снова нормально» не говорить.
    "slow_notified_at": None,
    "queue": None,
}


def failure_share(state: dict[str, Any]) -> int | None:
    """Доля неудач в окне, %. None — данных мало."""
    recent = state.get("recent") or []
    attempts = sum(int(a) for a, _ in recent)
    if attempts < DEGRADED_MIN_ATTEMPTS:
        return None
    failures = sum(int(f) for _, f in recent)
    return round(failures * 100 / attempts)


def _with_pass(state: dict[str, Any], attempted: int, failed: int) -> dict[str, Any]:
    if attempted <= 0:
        return state
    recent = list(state.get("recent") or [])
    recent.append([int(attempted), int(failed)])
    state["recent"] = recent[-DEGRADED_WINDOW:]
    share = failure_share(state)
    state["degraded"] = share is not None and share >= DEGRADED_RATIO * 100
    return state


def next_state(
    current: dict[str, Any] | None,
    outcome: str,
    reason: str | None,
    now: datetime,
    attempted: int = 0,
    failed: int = 0,
    fallback: str | None = None,
) -> dict[str, Any]:
    """Новое состояние по исходу прохода. Чистая функция; idle состояние не меняет."""
    state = dict(INITIAL if not current else current)

    if outcome == OUTCOME_IDLE:
        return state

    if outcome == OUTCOME_SUCCESS:
        # Окно частичных сбоев переживает успех: успех и деградация совместимы.
        return _with_pass(
            dict(
                INITIAL,
                status=STATUS_OK,
                since=state.get("since") if state.get("status") == STATUS_OK else None,
                recent=state.get("recent") or [],
                last_contact=now.isoformat(),
                fallback=fallback,
                # Скорость переживает удачный проход: ответ может быть и медленным.
                **{key: state.get(key, INITIAL[key]) for key in SPEED_KEYS},
            ),
            attempted,
            failed,
        )
    state["fallback"] = fallback
    state = _with_pass(state, attempted, failed)

    if outcome == OUTCOME_BUDGET:
        # Потолок токенов — не флуктуация, подтверждения не ждём.
        if state.get("status") != STATUS_DOWN:
            state["since"] = now.isoformat()
        state["status"] = STATUS_DOWN
        state["reason"] = reason or BUDGET_REASON
        state["consecutive_failures"] = FAILURE_THRESHOLD
        return state

    failures = int(state.get("consecutive_failures") or 0) + 1
    state["consecutive_failures"] = failures
    state["reason"] = reason
    if failures >= FAILURE_THRESHOLD and state.get("status") != STATUS_DOWN:
        state["status"] = STATUS_DOWN
        state["since"] = now.isoformat()
    return state


def probe_due(state: dict[str, Any], now: datetime) -> bool:
    last = state.get("last_contact")
    if not last:
        return True
    try:
        moment = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    return now - moment >= timedelta(minutes=PROBE_AFTER_MINUTES)


def last_contact_at(state: dict[str, Any]) -> datetime | None:
    return _moment(state.get("last_contact"))


def _moment(value: Any) -> datetime | None:
    """Момент из состояния. Сломанная строка — None, а не исключение."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def latency_window(state: dict[str, Any]) -> list[int]:
    window: list[int] = []
    for value in state.get("latencies") or []:
        try:
            window.append(int(value))
        except (TypeError, ValueError):
            continue
    return window


def latency_median_ms(state: dict[str, Any]) -> int | None:
    """Медиана окна, мс. None — окно пусто.

    Медиана, а не среднее: один вызов, упёршийся в таймаут, не делает провайдера медленным.
    """
    window = sorted(latency_window(state))
    if not window:
        return None
    middle = len(window) // 2
    if len(window) % 2:
        return window[middle]
    return (window[middle - 1] + window[middle]) // 2


def latency_max_ms(state: dict[str, Any]) -> int | None:
    window = latency_window(state)
    return max(window) if window else None


def slow_since_at(state: dict[str, Any]) -> datetime | None:
    return _moment(state.get("slow_since"))


def next_speed_state(
    current: dict[str, Any] | None,
    *,
    latencies_ms: Sequence[int] = (),
    pending: int = 0,
    chats: int = 0,
    oldest_minutes: int | None = None,
    now: datetime,
    latency_threshold_seconds: int,
    queue_threshold_minutes: int,
    suppressed: bool = False,
) -> tuple[dict[str, Any], str | None]:
    """Новое состояние скорости по итогам прохода. Чистая функция.

    Возвращает `(состояние, переход)`, переход — "slow" (вход), "slow_still"
    (не чаще раза в три часа), "slow_cleared" (только если про вход уже говорили)
    или None. `suppressed=True`: состояние ведётся, но переход наружу не отдаётся
    и «говорили про вход» не ставится.
    """
    state = dict(INITIAL if not current else current)

    window = latency_window(state)
    fresh_samples = [int(value) for value in latencies_ms if value is not None]
    if fresh_samples:
        window.extend(fresh_samples)
        state["latencies_at"] = now.isoformat()
    state["latencies"] = window[-LATENCY_WINDOW:]
    state["queue"] = {
        "pending": int(pending),
        "chats": int(chats),
        "oldest_minutes": None if oldest_minutes is None else int(oldest_minutes),
        "at": now.isoformat(),
    }

    threshold_ms = int(latency_threshold_seconds) * 1000
    measured_at = _moment(state.get("latencies_at"))
    fresh = measured_at is not None and now - measured_at <= timedelta(
        minutes=LATENCY_FRESH_MINUTES
    )
    median = latency_median_ms(state)
    enough = len(state["latencies"]) >= SLOW_MIN_SAMPLES
    latency_high = bool(fresh and enough and median is not None and median >= threshold_ms)
    queue_late = oldest_minutes is not None and int(oldest_minutes) >= int(
        queue_threshold_minutes
    )

    streak = int(state.get("slow_streak") or 0)
    state["slow_streak"] = streak + 1 if (latency_high or queue_late) else 0

    if not state.get("slow"):
        if state["slow_streak"] < SLOW_ENTRY_TICKS:
            return state, None
        state["slow"] = True
        state["slow_since"] = now.isoformat()
        state["slow_ok_since"] = None
        if suppressed:
            return state, None
        state["slow_notified_at"] = now.isoformat()
        return state, "slow"

    # Уже медленно. Выход — медиана ниже половины порога (гистерезис)
    # и очередь без просроченных сообщений.
    calm = (not fresh or median is None or median < threshold_ms // 2) and not queue_late
    if not calm:
        state["slow_ok_since"] = None
        notified = _moment(state.get("slow_notified_at"))
        if (
            notified is not None
            and not suppressed
            and now - notified >= timedelta(hours=SLOW_REMINDER_HOURS)
        ):
            state["slow_notified_at"] = now.isoformat()
            return state, "slow_still"
        return state, None

    if not state.get("slow_ok_since"):
        state["slow_ok_since"] = now.isoformat()
    calm_since = _moment(state.get("slow_ok_since"))
    if calm_since is None or now - calm_since < timedelta(minutes=SLOW_CLEAR_MINUTES):
        return state, None

    told_about_entry = bool(state.get("slow_notified_at"))
    state["slow"] = False
    state["slow_since"] = None
    state["slow_ok_since"] = None
    state["slow_streak"] = 0
    state["slow_notified_at"] = None
    if told_about_entry and not suppressed:
        return state, "slow_cleared"
    return state, None


async def record_speed(
    session: AsyncSession,
    *,
    latencies_ms: Sequence[int] = (),
    pending: int = 0,
    chats: int = 0,
    oldest_minutes: int | None = None,
    latency_threshold_seconds: int,
    queue_threshold_minutes: int,
    suppressed: bool = False,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str | None, datetime | None]:
    """Учесть скорость прохода. Возвращает (состояние, переход, начало сбоя).

    Начало сбоя отдаётся отдельно: при выходе `slow_since` в состоянии уже стёрт.
    """
    moment = now or datetime.now(timezone.utc)
    previous = await load_state(session)
    started_at = slow_since_at(previous)
    state, transition = next_speed_state(
        previous,
        latencies_ms=latencies_ms,
        pending=pending,
        chats=chats,
        oldest_minutes=oldest_minutes,
        now=moment,
        latency_threshold_seconds=latency_threshold_seconds,
        queue_threshold_minutes=queue_threshold_minutes,
        # Провайдер лежит целиком — о «медленно» поверх сообщения о сбое не говорим.
        suppressed=suppressed or previous.get("status") == STATUS_DOWN,
    )
    if state != previous:
        await save_state(session, state)
    if transition in ("slow", "slow_cleared"):
        log.warning(
            "ai_health.slow" if transition == "slow" else "ai_health.slow_cleared",
            median_ms=latency_median_ms(state),
            pending=pending,
            chats=chats,
            oldest_minutes=oldest_minutes,
        )
    return state, transition, started_at


async def load_state(session: AsyncSession) -> dict[str, Any]:
    stored = await session.get(Setting, STATE_KEY)
    if stored is None or not isinstance(stored.value, dict):
        return dict(INITIAL)
    return {**INITIAL, **stored.value}


async def save_state(session: AsyncSession, state: dict[str, Any]) -> None:
    stored = await session.get(Setting, STATE_KEY)
    if stored is None:
        session.add(Setting(key=STATE_KEY, value=state, updated_by=None))
    else:
        stored.value = state


async def record_outcome(
    session: AsyncSession,
    outcome: str,
    reason: str | None = None,
    attempted: int = 0,
    failed: int = 0,
    fallback: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Учесть исход прохода. Возвращает (состояние, переход "down"/"up" или None)."""
    now = datetime.now(timezone.utc)
    previous = await load_state(session)
    state = next_state(
        previous,
        outcome,
        reason,
        now,
        attempted=attempted,
        failed=failed,
        fallback=fallback,
    )
    if state != previous:
        await save_state(session, state)

    transition = None
    if previous.get("status") != state.get("status"):
        transition = "down" if state["status"] == STATUS_DOWN else "up"
        log.warning(
            "ai_health.changed",
            status=state["status"],
            reason=state.get("reason"),
        )
    return state, transition


async def alerts_paused(session: AsyncSession) -> tuple[bool, str | None]:
    """Нужно ли молчать алертам и почему."""
    state = await load_state(session)
    if state.get("status") == STATUS_DOWN:
        return True, state.get("reason")
    return False, None


# Признаки исчерпанного счёта в ответе провайдера (API баланса у Cloud.ru нет).
_BILLING_STATUSES = ("HTTP 402", "HTTP 403")
_BILLING_WORDS = (
    "balance", "insufficient", "quota", "payment", "billing", "credit",
    "funds", "баланс", "средств", "оплат", "квот", "лимит",
)


# Собственный потолок токенов — не отказ провайдера; `billing_hint` его не распознаёт.
BUDGET_REASON = "исчерпан месячный лимит токенов (AI_MONTHLY_TOKEN_LIMIT)"


def billing_hint(reason: str | None) -> str | None:
    """Похож ли отказ провайдера на исчерпанный счёт. None — не похож."""
    if not reason or str(reason).startswith(BUDGET_REASON):
        return None
    text = str(reason).lower()
    if any(status.lower() in text for status in _BILLING_STATUSES) or any(
        word in text for word in _BILLING_WORDS
    ):
        return (
            "Похоже, закончился баланс или лимит у провайдера ИИ (Cloud.ru) — "
            "проверьте счёт и пополните."
        )
    return None


_KEY_WORDS = (
    "key is expired", "accessdenied", "access denied", "api key", "api_key",
    "apikey", "invalid api key", "invalid_api_key", "unauthorized",
    "expired", "истёк", "истек", "ключ",
)
_KEY_STATUSES = ("http 401", "http 403")

_MODEL_WORDS = (
    "model not found", "model_not_found", "does not exist", "no such model",
)
_MODEL_STATUSES = ("http 404",)

_NETWORK_WORDS = (
    "timeout", "timed out", "connect", "name or service", "connectionerror",
)
_NETWORK_STATUSES = ("http 502", "http 503", "http 504")

_HINT_KEY = (
    "Истёк или отозван API-ключ провайдера ИИ (Cloud.ru)",
    "Создайте новый ключ в консоли Cloud.ru и замените его на сервере "
    "скриптом <code>scripts/set_ai_key.sh</code> "
    "(<code>ssh -t СЕРВЕР КАТАЛОГ_ПРОЕКТА/scripts/set_ai_key.sh</code>). "
    "До замены алерты стоят на паузе, сообщения записываются.",
)
_HINT_BILLING = (
    "Похоже, закончился баланс или лимит у провайдера ИИ (Cloud.ru)",
    "Проверьте счёт в консоли Cloud.ru и пополните его. До пополнения "
    "алерты стоят на паузе, сообщения записываются.",
)
_HINT_MODEL = (
    "Модель недоступна у провайдера (убрана из каталога или переименована)",
    "Проверьте каталог Cloud.ru и настройку AI_MODEL.",
)
_HINT_NETWORK = (
    "Провайдер ИИ не отвечает или отвечает ошибкой сервера",
    "Обычно проходит само; если держится дольше часа — проверьте статус "
    "Cloud.ru.",
)
_HINT_BUDGET = (
    "Исчерпан наш месячный потолок токенов (AI_MONTHLY_TOKEN_LIMIT)",
    "Это наша настройка, а не счёт провайдера: пополнять ничего не нужно. "
    "Классификация возобновится с началом месяца; нужна раньше — поднимите "
    "AI_MONTHLY_TOKEN_LIMIT на сервере.",
)

# Сколько сырого ответа провайдера показывать (ответ шлюза бывает HTML-страницей).
RAW_REASON_LIMIT = 200


def failure_hint(reason: str | None) -> tuple[str, str] | None:
    """(причина по-человечески, что делать) по сырому ответу провайдера; None — класс не узнан.

    Порядок проверок важен: голые 401/403 относятся к ключу, счёт узнаётся
    раньше только по словам про деньги и по 402.
    """
    if not reason:
        return None
    text = str(reason)
    low = text.lower()

    if text.startswith(BUDGET_REASON):
        return _HINT_BUDGET
    if any(word in low for word in _KEY_WORDS):
        return _HINT_KEY
    # Сужение `billing_hint` до слов про деньги и 402, чтобы 401/403 достались ключу.
    if billing_hint(text) and (
        "http 402" in low or any(word in low for word in _BILLING_WORDS)
    ):
        return _HINT_BILLING
    if any(status in low for status in _KEY_STATUSES):
        return _HINT_KEY
    if any(status in low for status in _MODEL_STATUSES) or any(
        word in low for word in _MODEL_WORDS
    ):
        return _HINT_MODEL
    if any(status in low for status in _NETWORK_STATUSES) or any(
        word in low for word in _NETWORK_WORDS
    ):
        return _HINT_NETWORK
    return None


def raw_reason_text(reason: str | None, limit: int = RAW_REASON_LIMIT) -> str:
    """Сырой ответ провайдера, обрезанный и безопасный для HTML.

    Обрезка до экранирования, иначе сущность может разорваться; `app.text.esc`,
    а не `html.escape`, чтобы не превращать кавычки JSON в «&quot;».
    """
    text = str(reason or "").strip()
    if not text:
        return ""
    return esc(text[:limit]) + ("…" if len(text) > limit else "")


def down_message(reason: str | None) -> str:
    hint = failure_hint(reason)
    if hint:
        why, todo = hint
        raw = raw_reason_text(reason)
        head = (
            f"<b>{why}</b>\n\n"
            f"Что делать: {todo}\n\n"
            + (f"<i>Ответ провайдера: {raw}</i>\n\n" if raw else "")
        )
    else:
        # Экранирование обязательно: «<» из HTML-страницы шлюза ломает разбор сообщения.
        head = f"Причина: {esc(reason or 'провайдер не отвечает')}\n\n"

    return (
        "⚠️ <b>ИИ-классификация недоступна</b>\n\n"
        + head
        + "<b>Алерты приостановлены.</b> Без классификации бот не отличает "
        "«спасибо» от вопроса и завалил бы вас ложными тревогами по всем чатам.\n\n"
        "Сообщения продолжают записываться, ничего не теряется: когда провайдер "
        "вернётся, всё пересчитается задним числом и пропущенные просрочки всплывут."
    )


def up_message() -> str:
    return (
        "✅ <b>ИИ-классификация снова работает.</b>\n\n"
        "Алерты возобновлены, накопившееся пересчитано."
    )


# Тексты про медленного провайдера — только ответственным за систему
# (`Perm.SYSTEM_HEALTH`), не руководителям. Текстов клиентских сообщений здесь нет.


def _plural(count: int, one: str, few: str, many: str) -> str:
    """«1 сообщение», «2 сообщения», «5 сообщений» — счёт читает человек."""
    number = abs(int(count))
    if number % 100 in range(11, 15):
        return many
    if number % 10 == 1:
        return one
    if number % 10 in (2, 3, 4):
        return few
    return many


def _seconds_text(milliseconds: int | None) -> str:
    if milliseconds is None:
        return "неизвестно"
    seconds = milliseconds / 1000
    if seconds < 10:
        return f"{seconds:.1f} с".replace(".", ",")
    return f"{round(seconds)} с"


def _duration_text(seconds: float | None) -> str:
    if seconds is None:
        return "неизвестно сколько"
    minutes = max(1, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} {_plural(minutes, 'минуту', 'минуты', 'минут')}"
    hours, rest = divmod(minutes, 60)
    text = f"{hours} {_plural(hours, 'час', 'часа', 'часов')}"
    return text if not rest else f"{text} {rest} мин"


def _queue_text(pending: int, chats: int, oldest_minutes: int | None) -> str:
    if not pending:
        return "Очередь разметки пуста."
    line = (
        f"Ждут разметки {pending} "
        f"{_plural(pending, 'сообщение', 'сообщения', 'сообщений')} "
        f"в {chats} {_plural(chats, 'чате', 'чатах', 'чатах')}"
    )
    if oldest_minutes is not None:
        line += (
            f", самое старое ждёт {oldest_minutes} "
            f"{_plural(oldest_minutes, 'минуту', 'минуты', 'минут')}"
        )
    return line + "."


def _skipped_text(count: int, chat_titles: Sequence[str]) -> str:
    """Сообщения, оставшиеся без разметки: их проверяют глазами."""
    if not count:
        return ""
    from app.services.ai_stats import MAX_CLASSIFY_ATTEMPTS

    attempts = MAX_CLASSIFY_ATTEMPTS
    titles = ", ".join(f"«{esc(title)}»" for title in chat_titles if title)
    return (
        f"\n\n⚠️ Без разметки осталось {count} "
        f"{_plural(count, 'сообщение', 'сообщения', 'сообщений')} — "
        f"бот спрашивал {attempts} {_plural(attempts, 'раз', 'раза', 'раз')} "
        "и не дождался ответа"
        + (f". Чаты: {titles}" if titles else "")
        + ". Их стоит просмотреть глазами: по ним бот ничего не подскажет."
    )


def _retry_text() -> str:
    from app.services.ai_stats import RETRY_BACKOFF_MINUTES

    steps = " и ".join(str(minutes) for minutes in RETRY_BACKOFF_MINUTES)
    return (
        f"<b>Что бот делает сам.</b> Повторяет разбор через {steps} "
        f"{_plural(RETRY_BACKOFF_MINUTES[-1], 'минуту', 'минуты', 'минут')}, "
        "последнюю попытку отдаёт резервной модели. Сообщения записаны, "
        "ничего не теряется — разметка догонит."
    )


def latency_text(state: dict[str, Any]) -> str:
    """Задержка провайдера для экрана состояния: медиана и объём окна."""
    window = latency_window(state)
    if not window:
        return "замеров пока нет"
    return (
        f"{_seconds_text(latency_median_ms(state))} "
        f"(медиана по {len(window)} "
        f"{_plural(len(window), 'вызову', 'вызовам', 'вызовам')}, "
        f"максимум {_seconds_text(latency_max_ms(state))})"
    )


def slow_message(
    *,
    median_ms: int | None,
    pending: int,
    chats: int,
    oldest_minutes: int | None,
) -> str:
    """Вход в состояние «медленно». Одно сообщение, без повторов."""
    return (
        "🐢 <b>Провайдер ИИ отвечает медленнее обычного</b>\n\n"
        f"Обычное время ответа сейчас {_seconds_text(median_ms)} "
        "(норма — около 1 секунды).\n"
        f"{_queue_text(pending, chats, oldest_minutes)}\n\n"
        "<b>Что это значит.</b> По этим чатам бот видит новые обращения "
        "с задержкой: он узнаёт о них позже, и алерты о просрочке тоже "
        "могут прийти позже, чем нужно.\n\n"
        f"{_retry_text()}\n\n"
        "<b>Что делать.</b> Ничего — это пройдёт само. Если держится долго, "
        "загляните в боте в раздел «🩺 Состояние системы»: там видно "
        "задержку, очередь и пропуски."
    )


def slow_still_message(
    *,
    median_ms: int | None,
    pending: int,
    chats: int,
    oldest_minutes: int | None,
    since_seconds: float | None,
    skipped: int = 0,
    skipped_chats: Sequence[str] = (),
) -> str:
    """«Всё ещё медленно» — не чаще раза в три часа."""
    return (
        "🐢 <b>Провайдер ИИ всё ещё отвечает медленно</b>\n\n"
        f"Длится уже {_duration_text(since_seconds)}.\n"
        f"Обычное время ответа сейчас {_seconds_text(median_ms)} "
        "(норма — около 1 секунды).\n"
        f"{_queue_text(pending, chats, oldest_minutes)}"
        + _skipped_text(skipped, skipped_chats)
        + "\n\n<b>Что делать.</b> Если это тянется часами — проверьте "
        "провайдера ИИ и его статус. Бот продолжает работать и всё "
        "записывает."
    )


def slow_cleared_message(
    *,
    median_ms: int | None,
    since_seconds: float | None,
    skipped: int = 0,
    skipped_chats: Sequence[str] = (),
) -> str:
    """Выход из состояния «медленно». Одно сообщение."""
    return (
        "✅ <b>Провайдер ИИ снова отвечает нормально, очередь разобрана</b>\n\n"
        f"Задержка держалась {_duration_text(since_seconds)}. "
        f"Время ответа сейчас {_seconds_text(median_ms)}, "
        "сообщений, ждущих разметки дольше обычного, нет."
        + _skipped_text(skipped, skipped_chats)
    )
