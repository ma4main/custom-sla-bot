"""Интервалы наблюдения за чатом: когда он был в анализе.

Отчёты фильтруют сообщения по пересечению с интервалами, а не по текущему состоянию
чата. Пауза и архив — «не наблюдаем»: сообщения этих промежутков в отчёты не попадают.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chat, ChatState, ChatTrackingPeriod, Message

log = structlog.get_logger(__name__)


def observed_filter(message=Message):
    """Условие «сообщение пришло, когда чат был под наблюдением». Группа уведомлений
    исключается здесь же: её история в базе не должна попадать в отчёты.
    """
    from app.config import notify_group_ids

    period = ChatTrackingPeriod
    condition = exists(
        select(period.id).where(
            and_(
                period.chat_id == message.chat_id,
                message.sent_at >= period.started_at,
                (period.ended_at.is_(None)) | (message.sent_at < period.ended_at),
            )
        )
    )
    excluded = notify_group_ids()
    if excluded:
        condition = and_(
            condition,
            message.chat_id.notin_(select(Chat.id).where(Chat.tg_chat_id.in_(excluded))),
        )
    return condition


async def open_period(
    session: AsyncSession, chat: Chat, *, reason: str, at: datetime | None = None
) -> None:
    """Начать интервал, если он ещё не открыт. «Один открытый интервал на чат» держит
    частичный уникальный индекс; гонка гасится точкой сохранения.
    """
    from sqlalchemy.exc import IntegrityError

    moment = at or datetime.now(timezone.utc)
    current = await session.scalar(
        select(ChatTrackingPeriod)
        .where(ChatTrackingPeriod.chat_id == chat.id)
        .where(ChatTrackingPeriod.ended_at.is_(None))
        .limit(1)
    )
    if current is not None:
        return
    try:
        async with session.begin_nested():
            session.add(
                ChatTrackingPeriod(chat_id=chat.id, started_at=moment, reason=reason)
            )
            await session.flush()
    except IntegrityError:
        log.info("tracking.open_race", chat_id=chat.id)
        return
    log.info("tracking.opened", chat_id=chat.id, reason=reason)


async def close_period(
    session: AsyncSession, chat: Chat, *, reason: str, at: datetime | None = None
) -> None:
    moment = at or datetime.now(timezone.utc)
    current = await session.scalar(
        select(ChatTrackingPeriod)
        .where(ChatTrackingPeriod.chat_id == chat.id)
        .where(ChatTrackingPeriod.ended_at.is_(None))
        .limit(1)
    )
    if current is None:
        return
    current.ended_at = moment
    current.reason = reason
    log.info("tracking.closed", chat_id=chat.id, reason=reason)


async def sync_period(
    session: AsyncSession, chat: Chat, state: ChatState, *, reason: str
) -> None:
    if state is ChatState.TRACKED:
        await open_period(session, chat, reason=reason)
    else:
        await close_period(session, chat, reason=reason)


async def backfill(session: AsyncSession) -> int:
    """Страховка для чатов без интервалов: интервал от `tracked_since` (или создания
    чата) до `archived_at`, приблизительный.
    """
    chats = (await session.scalars(select(Chat))).all()
    created = 0
    for chat in chats:
        has_any = await session.scalar(
            select(ChatTrackingPeriod.id)
            .where(ChatTrackingPeriod.chat_id == chat.id)
            .limit(1)
        )
        if has_any is not None:
            continue
        if chat.state is ChatState.DISCOVERED and chat.tracked_since is None:
            continue  # чат ни разу не наблюдался

        started = chat.tracked_since or chat.created_at
        ended = chat.archived_at if chat.state is not ChatState.TRACKED else None
        if chat.state is not ChatState.TRACKED and ended is None:
            # На паузе, но момент неизвестен: наблюдение закончилось сейчас,
            # чтобы прошлое осталось в отчётах.
            ended = datetime.now(timezone.utc)

        session.add(
            ChatTrackingPeriod(
                chat_id=chat.id,
                started_at=started,
                ended_at=ended,
                reason="backfill",
            )
        )
        created += 1

    if created:
        log.info("tracking.backfilled", chats=created)
    return created


def currently_tracked_chats():
    """Чаты, наблюдаемые прямо сейчас. Обращение, открытое до паузы, остаётся
    в отчётах, но ожиданием ответа (и поводом для алерта) быть перестаёт.
    """
    return (
        select(Chat.id)
        .join(ChatTrackingPeriod, ChatTrackingPeriod.chat_id == Chat.id)
        .where(Chat.state == ChatState.TRACKED)
        .where(ChatTrackingPeriod.ended_at.is_(None))
        .scalar_subquery()
    )
