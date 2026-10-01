"""Переход из числа отчёта в список конкретных обращений.

Готовые срезы (просроченные, ждут ответа, ждут специалиста, без ответа,
ответ не требовался); период наследуется из отчёта. Число на кнопке и список
считаются одними условиями (`_conditions`), чтобы они сходились.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Chat, Classification, Interaction, InteractionState, Message, Staff
from app.services.dismissals import dismissed_exists
from app.services.tracking import currently_tracked_chats
from app.services.verdicts import SOURCE_TECHNICAL

KIND_BREACH = "breach"
KIND_BREACH_REACTION = "brre"
KIND_BREACH_SPECIALIST = "brsp"
KIND_WAITING = "wait"
KIND_HANDOFF = "handoff"
KIND_NO_ANSWER = "noans"
KIND_NO_NEED = "noneed"

# KIND_BREACH — общий срез обеих просрочек; принимается из callback, кнопкой не показывается.
KIND_LABELS = {
    KIND_BREACH: "⚠️ Просроченные",
    KIND_BREACH_REACTION: "⚠️ Ответили с опозданием",
    KIND_BREACH_SPECIALIST: "⚠️ Специалист ответил с опозданием",
    KIND_WAITING: "⌛ Ответа так и нет",
    KIND_HANDOFF: "⏳ Ждут ответа специалиста",
    KIND_NO_ANSWER: "❓ Остались без ответа",
    KIND_NO_NEED: "💤 Ответ не требовался",
}


PER_PAGE = 6


def _conditions(kind: str, start: datetime, end: datetime, chat_id: int) -> list:
    conditions = [Interaction.opened_at >= start, Interaction.opened_at < end]
    if chat_id:
        conditions.append(Interaction.chat_id == chat_id)

    # Решение «не нарушение» снимает обращение из срезов нарушений и очередей
    # ожидания; «ответ не требовался» оно не затрагивает.
    not_dismissed = ~dismissed_exists()

    if kind == KIND_BREACH:
        # По флагам, а не пересчётом сроков в SQL: по ним же считает отчёт.
        conditions.append(
            or_(
                Interaction.sla_breached.is_(True),
                Interaction.substantive_breached.is_(True),
            )
        )
        conditions.append(not_dismissed)
    elif kind == KIND_BREACH_REACTION:
        conditions.append(Interaction.sla_breached.is_(True))
        conditions.append(not_dismissed)
    elif kind == KIND_BREACH_SPECIALIST:
        conditions.append(Interaction.substantive_breached.is_(True))
        conditions.append(not_dismissed)
    elif kind == KIND_WAITING:
        # Очереди ожидания — про сейчас: чат должен быть в анализе,
        # иначе пауза оставляла бы обращения вечно «ждущими».
        conditions.append(
            Interaction.state.in_([InteractionState.OPEN, InteractionState.REACTED])
        )
        conditions.append(Interaction.handoff_at.is_(None))
        conditions.append(Interaction.chat_id.in_(currently_tracked_chats()))
        conditions.append(not_dismissed)
    elif kind == KIND_HANDOFF:
        conditions.append(not_dismissed)
        conditions.append(Interaction.state == InteractionState.REACTED)
        conditions.append(Interaction.handoff_at.isnot(None))
        conditions.append(Interaction.chat_id.in_(currently_tracked_chats()))
    elif kind == KIND_NO_ANSWER:
        conditions.append(Interaction.state == InteractionState.ABANDONED)
        conditions.append(not_dismissed)
    elif kind == KIND_NO_NEED:
        conditions.append(Interaction.state == InteractionState.NO_RESPONSE_NEEDED)
    else:
        raise ValueError(f"Неизвестный срез: {kind}")

    return conditions


async def drill_counts(
    session: AsyncSession, start: datetime, end: datetime, chat_id: int = 0
) -> dict[str, int]:
    """Числа для кнопок под отчётом. Ноль — кнопка не показывается."""
    result: dict[str, int] = {}
    for kind in (
        KIND_BREACH,
        KIND_BREACH_REACTION,
        KIND_BREACH_SPECIALIST,
        KIND_WAITING,
        KIND_HANDOFF,
        KIND_NO_ANSWER,
        KIND_NO_NEED,
    ):
        result[kind] = (
            await session.scalar(
                select(func.count(Interaction.id)).where(
                    *_conditions(kind, start, end, chat_id)
                )
            )
            or 0
        )
    return result


async def drill_page(
    session: AsyncSession,
    kind: str,
    start: datetime,
    end: datetime,
    chat_id: int = 0,
    page: int = 0,
    per_page: int = PER_PAGE,
) -> tuple[list[dict[str, Any]], int]:
    """Страница среза: (строки, всего). Строка несёт всё для показа и кнопки."""
    total = (
        await session.scalar(
            select(func.count(Interaction.id)).where(*_conditions(kind, start, end, chat_id))
        )
        or 0
    )

    # Для передачи — кто пообещал специалиста, для остальных — первая реакция.
    staff_id_column = (
        Interaction.handoff_staff_id if kind == KIND_HANDOFF else Interaction.first_reaction_staff_id
    )

    # История — свежие сверху; очереди — сверху тот, кто ждёт дольше всех.
    if kind == KIND_HANDOFF:
        order = Interaction.handoff_at.asc()
    elif kind == KIND_WAITING:
        order = Interaction.opened_at.asc()
    else:
        order = Interaction.opened_at.desc()

    opener = aliased(Message)
    rows = (
        await session.execute(
            select(Interaction, Chat.title, Staff.full_name, opener.text)
            .join(Chat, Chat.id == Interaction.chat_id)
            .join(opener, opener.id == Interaction.opened_by_message_id)
            .outerjoin(Staff, Staff.id == staff_id_column)
            .where(*_conditions(kind, start, end, chat_id))
            .order_by(order)
            .offset(page * per_page)
            .limit(per_page)
        )
    ).all()

    items = [
        {
            "chat_id": interaction.chat_id,
            "title": title,
            "opened_at": interaction.opened_at,
            "opened_by_message_id": interaction.opened_by_message_id,
            "last_client_at": interaction.last_client_at,
            "client_messages": interaction.client_messages,
            "state": interaction.state,
            "sla_breached": bool(interaction.sla_breached),
            "substantive_breached": bool(interaction.substantive_breached),
            "handoff_at": interaction.handoff_at,
            "first_reaction_at": interaction.first_reaction_at,
            "staff_name": staff_name,
            "opener_text": opener_text,
            "verdict_source": None,
            "verdict_label": None,
        }
        for interaction, title, staff_name, opener_text in rows
    ]

    if kind == KIND_NO_NEED and items:
        # Причина закрытия — вердикт по открывающему сообщению (правило или модель, метка).
        await _attach_verdicts(session, items)

    return items, total


async def _attach_verdicts(session: AsyncSession, items: list[dict[str, Any]]) -> None:
    from app.config import get_settings

    message_ids = [item["opened_by_message_id"] for item in items]
    rows = (
        await session.execute(
            select(Classification.message_id, Classification.source, Classification.label)
            # Только принятые модели — те же вердикты, по которым построено
            # обращение; при нескольких версиях в dict остаётся последняя.
            .where(Classification.model.in_(get_settings().ai_accepted_models))
            .where(Classification.message_id.in_(message_ids))
            .where(Classification.error.is_(None))
            # Технический вердикт — не решение модели, причиной закрытия он не показывается.
            .where(Classification.source != SOURCE_TECHNICAL)
            .order_by(
                Classification.message_id,
                Classification.prompt_version.asc(),
                Classification.id.asc(),
            )
        )
    ).all()
    verdicts: dict[int, tuple[str, str | None]] = {}
    for message_id, source, label in rows:
        verdicts[message_id] = (source, label)
    for item in items:
        verdict = verdicts.get(item["opened_by_message_id"])
        if verdict is not None:
            item["verdict_source"], item["verdict_label"] = verdict
