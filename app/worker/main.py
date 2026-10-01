"""Процесс worker: фоновая обработка, отдельная от приёма апдейтов.

Тик раз в минуту: правки, классификация новых сообщений, пересборка обращений,
алерты и рассылки. Всё, что делает воркер, производно — его можно останавливать
в любой момент (docs/ARCHITECTURE.md, раздел 2).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import aliased

from app.health import beat, start_watchdog
from app.bot.guard import create_bot
from app.bot.main import setup_logging
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import (
    BusinessSide,
    Classification,
    Message,
)
from app.services.access import Perm, notification_recipients
from app.services.ai import (
    current_message_text,
    client_payload,
    company_payload,
    AiBudgetExceeded,
    AiClient,
    active_profile,
)
from app.services.ai_health import (
    OUTCOME_BUDGET,
    OUTCOME_FAILURE,
    OUTCOME_IDLE,
    OUTCOME_SUCCESS,
    alerts_paused,
    down_message,
    latency_median_ms,
    load_state,
    probe_due,
    record_outcome,
    record_speed,
    slow_cleared_message,
    slow_message,
    slow_still_message,
    up_message,
)
from app.services.ai_stats import pending_backlog, technical_verdicts
from app.services.attribution import attribute_message
from app.services.retention import apply_retention
from app.services.alerts import (
    process_alerts,
    retry_undelivered,
    strike_closed_alerts,
)
from app.services.alert_digest import maybe_send_alert_digest
from app.services.digest import (
    maybe_send_evening_digest,
    maybe_send_monthly_report,
    maybe_send_weekly_report,
)
from app.services.reprocess import replay_failed_updates, sync_integrator_change
from app.services.staff import maybe_send_link_nudge
from app.services.staff_roles import refresh_observed_roles
from app.services.verdicts import SOURCE_MODEL, SOURCE_RULE, SOURCE_TECHNICAL
from app.services.episodes import ENGINE_RULES_REVISION, ENGINE_VERSION, rebuild_interactions
from app.services.classify_context import prepare_contexts

log = structlog.get_logger("worker")

TICK_SECONDS = 60
CLASSIFY_BATCH = 20
# Дедлайн прохода классификации — заведомо меньше предела сторожа
# (health.MAX_AGE_SECONDS["worker"] = 300).
CLASSIFY_DEADLINE_SECONDS = 150

_last_daily_at: "datetime | None" = None

# Дешёвые правила до ИИ: очевидные случаи не стоят ни одного токена.
_ACK_MARKERS = {
    "спасибо",
    "спасибо!",
    "благодарю",
    "ок",
    "ok",
    "окей",
    "хорошо",
    "принято",
    "понял",
    "поняла",
    "понятно",
    "👍",
    "🙏",
    "+",
}


def rule_based_client(text: str) -> dict | None:
    lowered = text.strip().lower()
    if lowered in _ACK_MARKERS:
        return {"label": "ack", "requires_response": False}
    if len(lowered) <= 2:
        return {"label": "social", "requires_response": False}
    return None


async def reprocess_edited(batch: int = 50) -> int:
    """Сбросить производные данные у отредактированных сообщений: вердикт удаляется,
    атрибуция пересчитывается. Флаг гасится только после нового вердикта или сразу,
    если текста не осталось.
    Важно: сброс — один раз на флаг, а не каждый тик: иначе теряется счёт попыток строки отказа.
    """
    from sqlalchemy import delete, or_

    async with session_scope() as session:
        edited = (
            await session.scalars(
                select(Message)
                .where(Message.needs_reclassification.is_(True))
                .order_by(Message.id)
                .limit(batch)
            )
        ).all()
        if not edited:
            return 0

        ids = [message.id for message in edited]
        # Разрешающая строка (`error IS NULL`) есть — сброс ещё не делался, снимается всё.
        # Иначе удаляется только то, что старше правки (`edited_at`): строка отказа после неё —
        # текущий цикл повторов, её счёт попыток сохраняется. При пустом `edited_at`
        # `created_at < NULL` даёт NULL, и счёт тоже сохраняется.
        stale = aliased(Classification)
        reset_pending = (
            select(stale.id)
            .where(stale.message_id == Classification.message_id)
            .where(stale.error.is_(None))
            .exists()
        )
        edited_at = (
            select(Message.edited_at)
            .where(Message.id == Classification.message_id)
            .scalar_subquery()
        )
        await session.execute(
            delete(Classification)
            .where(Classification.message_id.in_(ids))
            .where(or_(reset_pending, Classification.created_at < edited_at))
        )

        # Атрибуция не зависит от ИИ и ручную разметку не затирает.
        terminal = 0
        for message in edited:
            if message.business_side is BusinessSide.COMPANY:
                await attribute_message(session, message)
            if not message.text:
                # Правка убрала текст: классифицировать нечего, очередь такое не берёт —
                # флаг снимается здесь, иначе висел бы вечно и занимал выборку.
                message.needs_reclassification = False
                terminal += 1

        log.info("reprocess.edited", count=len(ids), without_text=terminal)
        return len(ids)


def causal_pending_condition(accepted_models: list[str]):
    """Условие очереди: раньше в чате нет отказа без принятого успеха. Более поздние
    сообщения чата ждут, пока отказавшее не разрешится (в том числе через тики).
    """
    from sqlalchemy import and_, exists, or_
    from app.services.tracking import observed_filter

    failed_message = aliased(Message)
    failure = aliased(Classification)
    success = aliased(Classification)
    has_success = exists(
        select(success.id).where(
            success.message_id == failed_message.id,
            success.model.in_(accepted_models),
            success.error.is_(None),
        )
    ).correlate(failed_message)
    earlier_error = exists(
        select(failed_message.id)
        .join(failure, failure.message_id == failed_message.id)
        .where(failed_message.chat_id == Message.chat_id)
        .where(failure.model.in_(accepted_models), failure.error.isnot(None))
        .where(failed_message.business_side.in_([BusinessSide.CLIENT, BusinessSide.COMPANY]))
        .where(failed_message.text.isnot(None), observed_filter(failed_message), ~has_success)
        .where(or_(failed_message.sent_at < Message.sent_at,
                   and_(failed_message.sent_at == Message.sent_at, failed_message.id < Message.id)))
    ).correlate(Message)
    return ~earlier_error


async def classify_pending(client: AiClient) -> dict[str, int | str | None]:
    """Классифицировать сообщения без вердикта активной модели.

    Внешние вызовы идут вне транзакции: 1) короткая транзакция — выборка очереди и бюджет;
    2) правила и сеть без открытой транзакции; 3) запись каждого результата отдельной
    короткой транзакцией. Возвращает счётчики и исход прохода для ai_health.
    """
    settings = get_settings()
    # Тексты промпта и записываемая версия (`db_version`) берутся из профиля модели;
    # формат входа общий для всех профилей.
    profile = active_profile()
    client_system = profile.client_system
    company_system = profile.company_system
    db_prompt_version = profile.db_version
    done = 0
    by_rule = 0
    failed = 0
    attempted = 0
    # Здоровье провайдера доказывают только сетевые успехи, не вердикты правил.
    net_success = 0
    budget_hit = False
    last_error: str | None = None

    # ── Этап 1: что классифицировать и сколько бюджета осталось ──────────
    async with session_scope() as session:
        # Строки отказа не удаляются по таймеру: они считают попытки, пауза повтора —
        # `ai_stats.RETRY_BACKOFF_MINUTES`. Удаляет их успех в той же транзакции, что и вердикт.
        from app.services.ai_stats import (
            MAX_CLASSIFY_ATTEMPTS,
            attempts_column,
            pending_conditions,
        )

        pending = (
            await session.execute(
                select(
                    Message.id,
                    Message.chat_id,
                    Message.text,
                    Message.has_media,
                    Message.media_kind,
                    Message.business_side,
                    Message.sent_at,
                    Message.needs_reclassification,
                    Message.thread_id,
                    attempts_column(settings.ai_accepted_models).label("attempts"),
                )
                .where(*pending_conditions(settings.ai_accepted_models))
                .where(causal_pending_condition(settings.ai_accepted_models))
                .order_by(Message.sent_at, Message.id)
                .limit(CLASSIFY_BATCH)
            )
        ).all()

        # Один отсоединённый снимок; вход строится из строгого префикса событий
        # с уже записанными вердиктами.
        context_batch = await prepare_contexts(session, pending) if pending else None

        client.start_pass(await client.month_tokens(session))

    # ── Этап 2: правила и сеть, транзакции нет ──────────────────────────
    # Каждый результат пишется сразу; что не успели до дедлайна — возьмёт следующий тик.
    started = time.monotonic()
    usage_requests = 0
    deadline_hit = False
    handled = 0
    blocked_chats: set[int] = set()
    deferred = 0
    gave_up = 0
    # Длительности сетевых вызовов прохода, мс, успешных и отказавших — в окно `ai_health`.
    latencies_ms: list[int] = []
    fallback_used = False

    for row in pending:
        if row.chat_id in blocked_chats:
            deferred += 1
            continue
        message_id = row.id
        text = row.text
        business_side = row.business_side
        needs_reclassification = row.needs_reclassification
        # Хотя бы одно сообщение за проход — всегда: очередь должна двигаться.
        if handled and time.monotonic() - started > CLASSIFY_DEADLINE_SECONDS:
            deadline_hit = True
            log.warning("classify.deadline", seconds=CLASSIFY_DEADLINE_SECONDS, handled=handled)
            break
        handled += 1
        # Отказов по сообщению до этого прохода (`attempts` строки отказа); строка очереди
        # без этого поля считается без отказов.
        attempts_before = int(getattr(row, "attempts", 0) or 0)
        is_client = business_side is BusinessSide.CLIENT
        verdict: dict | None = None
        error: str | None = None
        usage: tuple[int, int] = (0, 0)
        # На резерве модель и версию промпта приносит сам вызов.
        verdict_model = settings.ai_model
        verdict_prompt_version = db_prompt_version

        source = SOURCE_MODEL
        if is_client:
            verdict = rule_based_client(text or "")
            if verdict is not None:
                by_rule += 1
                source = SOURCE_RULE

        context_ready = False
        if verdict is None:
            try:
                tail, open_items = await context_batch.context(row)
                context_ready = True
            except Exception as exc:  # noqa: BLE001 — ошибка уходит в запись
                # Провайдера не звали: это отказ сообщения, а не сбой ИИ — в счётчики
                # здоровья провайдера не идёт, попытка сообщения засчитывается как обычно.
                log.exception("classify.context_failed", message_id=message_id)
                error = f"{type(exc).__name__}: {exc}"[:300]

        if context_ready:
            open_request_ids = [item.message_id for item in open_items]
            attempted += 1
            # Не определено, пока вызов не начат: отказ при сборке входа длительности не имеет.
            call_started: float | None = None
            try:
                build = client_payload if is_client else company_payload
                current_text = current_message_text(
                    text,
                    # Строка очереди без полей медиа считается текстом без медиа.
                    has_media=getattr(row, "has_media", False),
                    media_kind=getattr(row, "media_kind", None),
                )
                payload = build(current_text, tail, open_items)
                # Подробный ответ: расход по битому JSON уже оплачен и попадает в счётчик.
                # Последняя попытка идёт на резервную модель; общий размыкатель при этом не трогается.
                last_try = attempts_before >= MAX_CLASSIFY_ATTEMPTS - 1
                # Длительность меряется здесь, включая разбор и резервную попытку; отказавшие
                # вызовы тоже попадают в окно — таймаут и есть самый медленный ответ.
                call_started = time.monotonic()
                detail = await client.classify_detailed(
                    client_system if is_client else company_system,
                    payload,
                    is_client=is_client,
                    # Модель может назвать только обращения из входа; чужой номер гасится в None.
                    open_request_ids=open_request_ids,
                    force_fallback=last_try,
                )
            except AiBudgetExceeded as exc:
                log.warning("classify.budget_exceeded")
                budget_hit = True
                last_error = str(exc)[:200]
                break
            except Exception as exc:  # noqa: BLE001 — ошибка уходит в запись
                if call_started is not None:
                    latencies_ms.append(int((time.monotonic() - call_started) * 1000))
                error = f"{type(exc).__name__}: {exc}"[:300]
                last_error = error[:200]
                failed += 1
            else:
                latencies_ms.append(int((time.monotonic() - call_started) * 1000))
                usage = tuple(detail.get("usage") or (0, 0))
                if usage[0] or usage[1]:
                    usage_requests += 1
                if detail.get("fallback"):
                    # На резерве расход и вердикт пишутся под резервной моделью и её версией промпта,
                    # иначе движок не узнал бы свою разметку (ai_accepted_models).
                    fallback_used = True
                    verdict_model = str(detail.get("model") or verdict_model)
                    verdict_prompt_version = int(
                        detail.get("prompt_db_version") or verdict_prompt_version
                    )
                if detail.get("error") is not None:
                    error = str(detail["error"])[:300]
                    last_error = error[:200]
                    failed += 1
                else:
                    verdict = detail["verdict"]
                    net_success += 1

        if error is None:
            done += 1

        # Попытки исчерпаны: пишется технический вердикт — строка без ошибки, которую движок
        # не читает (сообщение остаётся неразмеченным), а очередь считает разрешённой.
        exhausted = error is not None and attempts_before + 1 >= MAX_CLASSIFY_ATTEMPTS

        # ── Этап 3, по одному: расход, затем вердикт ────────────────────────
        # Расход первым: он уже потрачен, и потерять его — недосчитаться в потолке.
        async with session_scope() as session:
            if usage[0] or usage[1]:
                await client.record_usage(
                    session, usage[0], usage[1], requests=1, model=verdict_model
                )
            # Важно: сначала снять прежнюю строку отказа, потом писать новую: UNIQUE
            # (message_id, model, prompt_version). Строка отказа одна и переносит счёт попыток;
            # успех её стирает.
            await session.execute(
                delete(Classification)
                .where(Classification.message_id == message_id)
                .where(Classification.error.isnot(None))
            )
            session.add(
                Classification(
                    message_id=message_id,
                    model=verdict_model,
                    prompt_version=verdict_prompt_version,
                    source=source,
                    label=(verdict or {}).get("label"),
                    requires_response=(verdict or {}).get("requires_response"),
                    is_substantive=(verdict or {}).get("is_substantive"),
                    answers_request_id=(verdict or {}).get("answers_request_id"),
                    error=error,
                    attempts=attempts_before + 1 if error is not None else 1,
                )
            )
            if exhausted:
                gave_up += 1
                session.add(
                    Classification(
                        message_id=message_id,
                        model=verdict_model,
                        # Версия 0 — «вердикта не было»; не сталкивается с ключом строки отказа.
                        prompt_version=0,
                        source=SOURCE_TECHNICAL,
                        error=None,
                    )
                )
                log.warning(
                    "classify.gave_up",
                    message_id=message_id,
                    chat_id=row.chat_id,
                    attempts=attempts_before + 1,
                    error=error,
                )
            if needs_reclassification and (error is None or exhausted):
                # Правка обработана. Исчерпание попыток — тоже терминальный исход: иначе флаг
                # возвращал бы сообщение в выборку `reprocess_edited` вечно.
                message = await session.get(Message, message_id)
                if message is not None:
                    message.needs_reclassification = False

        # Публикуем только после успешной записи; сетевой отказ оставляет прежний вердикт.
        if error is None and verdict is not None:
            context_batch.record_verdict(message_id, verdict, source)
        if error is not None and not exhausted:
            # Неизвестное сообщение компании может закрыть обращение эвристикой, поэтому
            # последующие сообщения чата ждут его разметки. Исчерпанное сообщение чат не блокирует.
            blocked_chats.add(row.chat_id)

    if budget_hit:
        outcome = OUTCOME_BUDGET
    elif net_success:
        outcome = OUTCOME_SUCCESS
    elif attempted and failed >= attempted:
        outcome = OUTCOME_FAILURE
    else:
        # Провайдера не звали — о его здоровье это ничего не говорит.
        outcome = OUTCOME_IDLE

    ordered = sorted(latencies_ms)
    return {
        "classified": done,
        "by_rule": by_rule,
        "failed": failed,
        "attempted": attempted,
        "outcome": outcome,
        "reason": last_error,
        "deadline_hit": deadline_hit,
        "deferred": deferred,
        "gave_up": gave_up,
        "latency_ms_median": (
            ordered[len(ordered) // 2]
            if len(ordered) % 2
            else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) // 2
        )
        if ordered
        else None,
        "latency_ms_max": ordered[-1] if ordered else None,
        "latencies_ms": latencies_ms,
        "fallback": getattr(client, "fallback_model", None) if fallback_used else None,
    }


async def notify_owners(bot, text: str) -> None:
    """Сообщить о состоянии системы. Адресаты — по праву «состояние системы», а не по роли."""
    async with session_scope() as session:
        people = await notification_recipients(session, Perm.SYSTEM_HEALTH)
        recipients = [user.tg_user_id for user in people]

    for tg_user_id in recipients:
        try:
            await bot.send_message(tg_user_id, text, parse_mode="HTML")
        except Exception:  # noqa: BLE001 — недоставленное не роняет тик
            log.exception("worker.notify_failed", tg_user_id=tg_user_id)


async def _record_slow_state(session, settings, stats, *, suppressed: bool) -> str | None:
    """Отставание, состояние «медленно» и текст уведомления (или None). Сам не отправляет:
    отправка идёт вне транзакции.
    """
    now = datetime.now(timezone.utc)
    pending, chats, oldest_at = await pending_backlog(
        session, settings.ai_accepted_models, now=now
    )
    oldest_minutes = (
        None
        if oldest_at is None
        else max(0, int((now - oldest_at).total_seconds() // 60))
    )
    state, transition, started_at = await record_speed(
        session,
        latencies_ms=stats.get("latencies_ms") or (),
        pending=pending,
        chats=chats,
        oldest_minutes=oldest_minutes,
        latency_threshold_seconds=settings.ai_slow_latency_seconds,
        queue_threshold_minutes=settings.ai_slow_queue_minutes,
        suppressed=suppressed,
        now=now,
    )
    if transition is None:
        return None

    median = latency_median_ms(state)
    if transition == "slow":
        return slow_message(
            median_ms=median,
            pending=pending,
            chats=chats,
            oldest_minutes=oldest_minutes,
        )

    since_seconds = None if started_at is None else (now - started_at).total_seconds()
    skipped, skipped_chats = await technical_verdicts(
        session, started_at or (now - timedelta(days=1))
    )
    if transition == "slow_still":
        return slow_still_message(
            median_ms=median,
            pending=pending,
            chats=chats,
            oldest_minutes=oldest_minutes,
            since_seconds=since_seconds,
            skipped=skipped,
            skipped_chats=skipped_chats,
        )
    return slow_cleared_message(
        median_ms=median,
        since_seconds=since_seconds,
        skipped=skipped,
        skipped_chats=skipped_chats,
    )


async def run_once(bot=None) -> None:
    settings = get_settings()

    # Правки — до классификации: сообщение возвращается в очередь без старого вердикта.
    await reprocess_edited()

    if settings.ai_enabled and settings.ai_api_key:
        async with AiClient() as client:
            # На резерве периодически пробуем основную минимальным (платным) вызовом —
            # до классификации, чтобы удачная проба вернула проход на основную сразу.
            probe_result = await client.maybe_probe_primary()
            if probe_result is not None:
                probe_usage = probe_result.get("usage") or (0, 0)
                if probe_usage[0] or probe_usage[1]:
                    async with session_scope() as session:
                        await client.record_usage(
                            session, probe_usage[0], probe_usage[1], requests=1
                        )
            stats = await classify_pending(client)
            if stats["outcome"] == OUTCOME_IDLE:
                # Очередь пуста и живого контакта давно не было — бесплатный GET /models,
                # чтобы сбой провайдера не ждал первого сообщения. Исход идёт в ту же машину состояний.
                async with session_scope() as session:
                    health = await load_state(session)
                if probe_due(health, datetime.now(timezone.utc)):
                    probe = await client.probe_detailed()
                    error = probe["error"]
                    stats = {
                        **stats,
                        "outcome": OUTCOME_FAILURE if error else OUTCOME_SUCCESS,
                        "reason": error,
                        "attempted": 1,
                        "failed": 1 if error else 0,
                        # Основная пропала, резерв в каталоге: провайдер жив, алерты не глушим.
                        "fallback": probe.get("fallback") or stats.get("fallback"),
                    }
                    if error:
                        log.warning("ai.probe_failed", error=error)
        if stats["classified"] or stats["failed"]:
            log.info(
                "classify.tick",
                **{key: value for key, value in stats.items() if key != "latencies_ms"},
            )

        # Здоровье провайдера решает, слать ли алерты: без вердиктов пошли бы ложные тревоги.
        async with session_scope() as session:
            _, transition = await record_outcome(
                session,
                str(stats["outcome"]),
                stats.get("reason"),
                attempted=int(stats.get("attempted") or 0),
                failed=int(stats.get("failed") or 0),
                fallback=stats.get("fallback"),
            )
            # Скорость — отдельное состояние поверх того же ключа `ai_health`: медленный
            # провайдер «здоров», но сдвигает разметку и алерты.
            slow_notice = await _record_slow_state(
                session,
                settings,
                stats,
                # На тике со своим сообщением о сбое/восстановлении уведомление о скорости не шлём.
                suppressed=transition is not None,
            )
        if bot is not None and transition == "down":
            state_reason = stats.get("reason")
            await notify_owners(bot, down_message(state_reason))
        elif bot is not None and transition == "up":
            await notify_owners(bot, up_message())
        if bot is not None and slow_notice:
            await notify_owners(bot, slow_notice)

    async with session_scope() as session:
        await rebuild_interactions(session)

    global _last_daily_at
    now = datetime.now(timezone.utc)
    if _last_daily_at is None or now - _last_daily_at >= timedelta(days=1):
        # Отметка ставится до запуска: упавшая задача повторится через сутки, а не каждым
        # тиком. Каждая задача — своей транзакцией и своим `try`: сбой одной не останавливает
        # ни соседние, ни пульс и алерты ниже.
        _last_daily_at = now
        # Упавшие апдейты переигрываются сами: повтор идемпотентен, нажатия кнопок
        # не переигрываются.
        for name, task in (
            ("retention", apply_retention),
            ("replay_failed_updates", replay_failed_updates),
            ("refresh_observed_roles", refresh_observed_roles),
        ):
            try:
                async with session_scope() as session:
                    await task(session)
            except Exception:  # noqa: BLE001 — суточная задача не роняет тик
                log.exception("worker.daily_task_failed", task=name)

    # Отметка для healthcheck — после обработки данных: зависший на середине тик не здоров.
    beat("worker")

    if bot is not None:
        async with session_scope() as session:
            paused, reason = await alerts_paused(session)
            if paused:
                log.warning("alerts.paused", reason=reason)
            else:
                # Сначала недоставленное: алерт, не ушедший из-за сети, важнее нового.
                recovered = await retry_undelivered(session, bot)
                if recovered:
                    log.info("alerts.recovered", count=recovered)
                await process_alerts(session, bot)
                # Зачёркивание отработанных алертов — после отправки новых.
                struck = await strike_closed_alerts(session, bot)
                if struck:
                    log.info("alerts.struck_total", count=struck)

        # Рассылки сами решают, пора ли, и сами управляют транзакциями (бронь выпуска
        # коммитится до вызова Telegram). Сначала досылка недоставленного, потом новые выпуски.
        from app.services.digest import retry_undelivered_issues

        await retry_undelivered_issues(bot)
        await maybe_send_evening_digest(bot)
        await maybe_send_weekly_report(bot)
        await maybe_send_monthly_report(bot)
        await maybe_send_alert_digest(bot)
        await maybe_send_link_nudge(bot)

    # Пульс — последним: отметка доказывает работу тика (services/uptime.py).
    from app.services.uptime import record_beat

    async with session_scope() as session:
        await record_beat(session, datetime.now(timezone.utc))


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    log.info(
        "worker.starting",
        ai_enabled=settings.ai_enabled,
        shadow_mode=settings.ai_shadow_mode,
        model=settings.ai_model,
        tick_seconds=TICK_SECONDS,
        engine=f"v{ENGINE_VERSION} rules {ENGINE_RULES_REVISION}",
    )

    # Бот с заслоном: запись в группы отрежет HTTP-слой даже при ошибке в коде.
    bot = create_bot(settings.require_bot_token())
    # Сторож живости (app/health.py): docker на unhealthy не реагирует.
    start_watchdog("worker")

    async with session_scope() as session:
        await sync_integrator_change(session)
        from app.services.notify_group import load_migration

        await load_migration(session)

        from app.services.settings_store import get_section
        from app.services.transcript import calendar_tz
        from app.services.uptime import detect_gap, downtime_message

        gap = await detect_gap(session, datetime.now(timezone.utc))
        tz = calendar_tz(await get_section(session, "work_calendar"))
    if gap is not None:
        since, until = gap
        log.warning(
            "worker.downtime_detected",
            since=since.isoformat(),
            minutes=int((until - since).total_seconds() // 60),
        )
        await notify_owners(bot, downtime_message(since, until, tz))
    try:
        while True:
            started = time.monotonic()
            try:
                await run_once(bot)
            except Exception:  # noqa: BLE001 — воркер не должен умирать от одной ошибки
                log.exception("worker.tick_failed")
            elapsed = time.monotonic() - started
            if elapsed > TICK_SECONDS / 2:
                log.warning("worker.tick_slow", seconds=round(elapsed, 1))
            else:
                log.debug("worker.tick_done", seconds=round(elapsed, 1))
            await asyncio.sleep(max(0.0, TICK_SECONDS - elapsed))
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
