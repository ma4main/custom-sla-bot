"""Решения «снять нарушение» — снятие формальных просрочек руками.

Обращение с решением не показывается в срезах нарушений и не считается в счётчиках
просрочек; ABANDONED учитывается как «ответа не требовалось». Само обращение
и метрики скорости не меняются; решение обратимо и пишется в журнал действий.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import and_, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditLog,
    BotUser,
    BreachDismissal,
    Chat,
    Interaction,
    InteractionState,
    Message,
)
from app.services.access import Perm, require_perm

log = structlog.get_logger(__name__)

# Живое обращение: ответа ещё нет ни в каком виде.
OPEN_STATES = (InteractionState.OPEN, InteractionState.REACTED)

PER_PAGE = 6


def dismissed_exists():
    """EXISTS «по обращению есть решение „не нарушение“» по (chat_id, opened_by_message_id) —
    ключу, который переживает пересборку.
    """
    return exists(
        select(BreachDismissal.id).where(
            and_(
                BreachDismissal.chat_id == Interaction.chat_id,
                BreachDismissal.opened_by_message_id == Interaction.opened_by_message_id,
            )
        )
    )


def live_waiting():
    """«Ждут ответа» — живое обращение без решения «не нарушение». Одно условие
    на все места, где показывается ожидание: число над кнопкой и список обязаны сходиться.
    """
    return and_(Interaction.state.in_(OPEN_STATES), ~dismissed_exists())


async def is_dismissed(
    session: AsyncSession, chat_id: int, opened_by_message_id: int
) -> bool:
    found = await session.scalar(
        select(BreachDismissal.id)
        .where(BreachDismissal.chat_id == chat_id)
        .where(BreachDismissal.opened_by_message_id == opened_by_message_id)
        .limit(1)
    )
    return found is not None


async def dismiss(
    session: AsyncSession, actor: BotUser, chat_id: int, opened_by_message_id: int
) -> bool:
    """Пометить обращение как «не нарушение». False — уже помечено."""
    require_perm(actor, Perm.REPORT_ALL_CHATS)

    if await is_dismissed(session, chat_id, opened_by_message_id):
        return False

    try:
        # SAVEPOINT: двойное нажатие между проверкой и вставкой упирается в уникальный
        # ключ — это «уже снято», а не падение транзакции обработчика.
        async with session.begin_nested():
            session.add(
                BreachDismissal(
                    chat_id=chat_id,
                    opened_by_message_id=opened_by_message_id,
                    dismissed_by=actor.id,
                )
            )
            await session.flush()
    except IntegrityError:
        return False
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="breach.dismissed",
            object_type="interaction",
            object_id=str(opened_by_message_id),
            payload={"chat_id": chat_id},
        )
    )
    log.info(
        "breach.dismissed",
        chat_id=chat_id,
        opened_by_message_id=opened_by_message_id,
        actor=actor.tg_user_id,
    )
    return True


async def restore(
    session: AsyncSession, actor: BotUser, chat_id: int, opened_by_message_id: int
) -> bool:
    require_perm(actor, Perm.REPORT_ALL_CHATS)

    row = await session.scalar(
        select(BreachDismissal)
        .where(BreachDismissal.chat_id == chat_id)
        .where(BreachDismissal.opened_by_message_id == opened_by_message_id)
    )
    if row is None:
        return False

    await session.delete(row)
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="breach.restored",
            object_type="interaction",
            object_id=str(opened_by_message_id),
            payload={"chat_id": chat_id},
        )
    )
    log.info(
        "breach.restored",
        chat_id=chat_id,
        opened_by_message_id=opened_by_message_id,
        actor=actor.tg_user_id,
    )
    return True


async def dismissed_page(
    session: AsyncSession,
    start: datetime,
    end: datetime,
    page: int = 0,
    per_page: int = PER_PAGE,
) -> tuple[list[dict[str, Any]], int]:
    """Журнал снятий за период: (строки, всего), свежие сверху. Период — по моменту
    решения. Обращение может отсутствовать после пересборки — поэтому outerjoin.
    """
    period = (
        BreachDismissal.dismissed_at >= start,
        BreachDismissal.dismissed_at < end,
    )
    total = (
        await session.scalar(
            select(func.count(BreachDismissal.id)).where(*period)
        )
        or 0
    )

    rows = (
        await session.execute(
            select(
                BreachDismissal,
                Chat.title,
                Message.text,
                Message.sent_at,
                BotUser.display_name,
                BotUser.username,
                BotUser.tg_user_id,
                Interaction.state,
            )
            .join(Chat, Chat.id == BreachDismissal.chat_id)
            .outerjoin(Message, Message.id == BreachDismissal.opened_by_message_id)
            .outerjoin(BotUser, BotUser.id == BreachDismissal.dismissed_by)
            .outerjoin(
                Interaction,
                (Interaction.chat_id == BreachDismissal.chat_id)
                & (
                    Interaction.opened_by_message_id
                    == BreachDismissal.opened_by_message_id
                ),
            )
            .where(*period)
            .order_by(BreachDismissal.dismissed_at.desc())
            .offset(page * per_page)
            .limit(per_page)
        )
    ).all()

    return [
        {
            "chat_id": row[0].chat_id,
            "title": row[1],
            "opened_by_message_id": row[0].opened_by_message_id,
            "opener_text": row[2],
            "opened_at": row[3],
            "dismissed_at": row[0].dismissed_at,
            # Пользователя могли удалить: показываем «неизвестно кем», решение не прячем.
            "who": row[4] or row[5] or (str(row[6]) if row[6] else None),
            "state": row[7],
        }
        for row in rows
    ], total
