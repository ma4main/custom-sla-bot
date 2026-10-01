"""Цифры про ИИ для экрана «Состояние системы».

Только чтение. Условия очереди общие с воркером: число на экране совпадает
с тем, что воркер возьмёт следующим тиком.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import AiUsage, BusinessSide, Chat, ChatState, Classification, Message
from app.services.verdicts import SOURCE_TECHNICAL

# Минуты ожидания перед n-м повтором после отказа провайдера. Длина кортежа
# задаёт число повторов: всего `len(RETRY_BACKOFF_MINUTES) + 1` попыток, затем
# сообщение закрывается техническим вердиктом (`verdicts.SOURCE_TECHNICAL`),
# иначе причинная очередь держала бы за ним весь чат.
RETRY_BACKOFF_MINUTES: tuple[int, ...] = (5, 15)
MAX_CLASSIFY_ATTEMPTS = len(RETRY_BACKOFF_MINUTES) + 1


async def month_tokens(session: AsyncSession) -> int:
    """Токены за текущий календарный месяц (UTC) — тот же счёт, что у потолка."""
    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    used = await session.scalar(
        select(func.coalesce(func.sum(AiUsage.prompt_tokens + AiUsage.completion_tokens), 0))
        .where(AiUsage.day >= month_start)
    )
    return int(used or 0)


def attempts_column(models: str | Sequence[str]):
    """Сколько отказов подряд уже было по сообщению (скалярный подзапрос).

    Строка отказа одна на (message_id, model, prompt_version) и хранит счётчик
    `attempts`; `max`, а не `sum`: отказ резервной модели лежит отдельной строкой.
    """
    accepted = [models] if isinstance(models, str) else list(models)
    failure = aliased(Classification)
    return (
        select(func.coalesce(func.max(failure.attempts), 0))
        .where(
            failure.message_id == Message.id,
            failure.model.in_(accepted),
            failure.error.isnot(None),
        )
        .correlate(Message)
        .scalar_subquery()
    )


def _retry_due(models: str | Sequence[str], now: datetime, backoff: Sequence[int]):
    """Отказ отлежал свой срок: n-й повтор ждёт `backoff[n-1]` минут."""
    accepted = [models] if isinstance(models, str) else list(models)
    failure = aliased(Classification)
    last_failure = (
        select(func.max(failure.created_at))
        .where(
            failure.message_id == Message.id,
            failure.model.in_(accepted),
            failure.error.isnot(None),
        )
        .correlate(Message)
        .scalar_subquery()
    )
    attempts = attempts_column(accepted)
    return or_(
        *[
            and_(attempts == index + 1, last_failure <= now - timedelta(minutes=minutes))
            for index, minutes in enumerate(backoff)
        ]
    )


def pending_conditions(
    models: str | Sequence[str],
    *,
    now: datetime | None = None,
    backoff: Sequence[int] = RETRY_BACKOFF_MINUTES,
) -> list:
    """Условия «сообщение ждёт вердикта» — общие для воркера и экрана.

    Правка сообщения (needs_reclassification) классифицируется и в чате
    на паузе или в архиве. Из очереди выводит только вердикт без `error`;
    строка с ошибкой возвращает сообщение в очередь по сроку `backoff`.
    """
    # Важно: вердикт любой версии промпта и любой из `models` закрывает сообщение:
    # новые правила действуют вперёд, прошлое не переписывается. Осознанный
    # переспрос — `scripts/reclassify.py` (ставит needs_reclassification).
    accepted = [models] if isinstance(models, str) else list(models)
    resolution = aliased(Classification)
    resolved = exists(
        select(resolution.id).where(
            resolution.message_id == Message.id,
            resolution.model.in_(accepted),
            resolution.error.is_(None),
        )
    ).correlate(Message)
    attempts = attempts_column(accepted)
    moment = now or datetime.now(timezone.utc)
    tracked_ids = select(Chat.id).where(Chat.state == ChatState.TRACKED).scalar_subquery()
    return [
        or_(Message.chat_id.in_(tracked_ids), Message.needs_reclassification.is_(True)),
        Message.business_side.in_([BusinessSide.CLIENT, BusinessSide.COMPANY]),
        ~resolved,
        or_(attempts == 0, _retry_due(accepted, moment, backoff)),
        Message.text.isnot(None),
    ]


async def pending_count(session: AsyncSession, models: str | Sequence[str]) -> int:
    count = await session.scalar(select(func.count(Message.id)).where(*pending_conditions(models)))
    return int(count or 0)


async def pending_backlog(
    session: AsyncSession,
    models: str | Sequence[str],
    *,
    now: datetime | None = None,
) -> tuple[int, int, datetime | None]:
    """Отставание очереди одним агрегатом: (сообщений, чатов, время самого старого).

    Причинного фильтра (`worker.causal_pending_condition`) здесь нет намеренно:
    он прячет чаты, стоящие за отказавшим сообщением. Возраст — от `sent_at`.
    """
    row = (
        await session.execute(
            select(
                func.count(Message.id),
                func.count(func.distinct(Message.chat_id)),
                func.min(Message.sent_at),
            ).where(*pending_conditions(models, now=now or datetime.now(timezone.utc)))
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0), row[2]


async def technical_verdicts(
    session: AsyncSession,
    since: datetime,
    *,
    limit: int = 5,
) -> tuple[int, list[str]]:
    """Сообщения с техническим вердиктом (без разметки): (сколько, названия чатов)."""
    rows = (
        await session.execute(
            select(Chat.title, func.count(Classification.id))
            .select_from(Classification)
            .join(Message, Message.id == Classification.message_id)
            .join(Chat, Chat.id == Message.chat_id)
            .where(
                Classification.source == SOURCE_TECHNICAL,
                Classification.created_at >= since,
            )
            .group_by(Chat.title)
            .order_by(func.count(Classification.id).desc())
        )
    ).all()
    total = sum(int(count) for _, count in rows)
    return total, [str(title) for title, _ in rows[:limit] if title]


async def usage_by_day(session: AsyncSession, days: int = 7) -> list:
    """Расход по дням и моделям за последние `days` дней, свежие первыми."""
    since = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=days - 1)
    rows = await session.execute(
        select(
            AiUsage.day,
            AiUsage.model,
            func.sum(AiUsage.requests),
            func.sum(AiUsage.prompt_tokens),
            func.sum(AiUsage.completion_tokens),
        )
        .where(AiUsage.day >= since)
        .group_by(AiUsage.day, AiUsage.model)
        .order_by(AiUsage.day.desc(), AiUsage.model)
    )
    return rows.all()


def cost_rub(
    prompt_tokens: int,
    completion_tokens: int,
    price_in: float | None,
    price_out: float | None,
) -> float | None:
    """Стоимость по ценам «₽ за миллион токенов». None — цены не заданы."""
    if price_in is None or price_out is None:
        return None
    return prompt_tokens / 1e6 * price_in + completion_tokens / 1e6 * price_out


async def last_verdict_at(session: AsyncSession) -> datetime | None:
    """Время последнего вердикта; ошибочные и технические записи не считаются."""
    return await session.scalar(
        select(func.max(Classification.created_at)).where(
            Classification.error.is_(None),
            Classification.source != SOURCE_TECHNICAL,
        )
    )
