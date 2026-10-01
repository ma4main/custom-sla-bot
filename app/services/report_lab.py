"""Данные для оперативных отчётов: где сейчас плохо (отклонения, а не объёмы).

Только чтение: на основные метрики и алерты не влияет.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Float, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Attribution,
    BusinessSide,
    Chat,
    ChatTrackingPeriod,
    Interaction,
    InteractionState,
    Message,
    Staff,
)
from app.services.calendar import (
    CalendarHistory,
    business_seconds,
    response_deadline,
)
from app.services.dismissals import OPEN_STATES, dismissed_exists, live_waiting
from app.services.tracking import currently_tracked_chats, observed_filter


def _real_breach():
    """Просрочка, не снятая решением «снять нарушение». Одно условие на все отчёты модуля."""
    from sqlalchemy import and_

    return and_(Interaction.sla_breached.is_(True), ~dismissed_exists())


def _observed_chats():
    return select(ChatTrackingPeriod.chat_id).distinct().scalar_subquery()


async def attention_now(
    session: AsyncSession, calendar_cfg: dict[str, Any], now: datetime, limit_minutes: int
) -> list[dict[str, Any]]:
    """Обращения, ждущие ответа прямо сейчас, самые старые сверху (срез, не период)."""
    rows = (
        await session.execute(
            select(Interaction, Chat.title)
            .join(Chat, Chat.id == Interaction.chat_id)
            # Только чаты, которые сейчас в анализе: обращение, открытое до паузы,
            # ожиданием быть перестаёт.
            .where(Interaction.chat_id.in_(currently_tracked_chats()))
            .where(Interaction.state.in_(OPEN_STATES))
            # Снятое решением «не нарушение» из очереди внимания уходит.
            .where(~dismissed_exists())
            .order_by(Interaction.opened_at)
        )
    ).all()

    # Срок и возраст — по графику, действовавшему в момент обращения (как у движка и алертов).
    calendar = CalendarHistory(calendar_cfg)

    items: list[dict[str, Any]] = []
    for interaction, title in rows:
        case_cfg = calendar.at(interaction.opened_at)
        deadline = response_deadline(
            interaction.opened_at, limit_minutes * 60, case_cfg
        )
        items.append(
            {
                "chat_id": interaction.chat_id,
                "title": title,
                "opened_at": interaction.opened_at,
                "opened_by_message_id": interaction.opened_by_message_id,
                "last_client_at": interaction.last_client_at,
                "client_messages": interaction.client_messages,
                "reacted": interaction.state is InteractionState.REACTED,
                "first_reaction_at": interaction.first_reaction_at,
                "business_age": business_seconds(interaction.opened_at, now, case_cfg),
                "deadline": deadline,
                "overdue": bool(deadline is not None and now > deadline),
            }
        )
    # Просроченные вперёд, внутри группы — кто ждёт дольше.
    items.sort(key=lambda item: (not item["overdue"], item["opened_at"]))
    return items


async def staff_speed(
    session: AsyncSession, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Скорость по сотрудникам: медиана первой реакции и просрочки."""
    rows = (
        await session.execute(
            select(
                Staff.id,
                Staff.full_name,
                Staff.role,
                func.count().label("episodes"),
                func.percentile_cont(0.5)
                .within_group(Interaction.ttfr_business_seconds.asc())
                .label("median"),
                func.percentile_cont(0.9)
                .within_group(Interaction.ttfr_business_seconds.asc())
                .label("p90"),
                func.count()
                .filter(_real_breach())
                .label("breached"),
            )
            .join(Staff, Staff.id == Interaction.first_reaction_staff_id)
            .where(Interaction.chat_id.in_(_observed_chats()))
            .where(Interaction.opened_at >= start, Interaction.opened_at < end)
            .where(Interaction.ttfr_business_seconds.isnot(None))
            .group_by(Staff.id, Staff.full_name, Staff.role)
            .order_by(func.count().filter(_real_breach()).desc())
        )
    ).all()

    return [
        {
            "staff_id": row.id,
            "full_name": row.full_name,
            "role": row.role,
            "episodes": row.episodes,
            "median": int(row.median) if row.median is not None else None,
            "p90": int(row.p90) if row.p90 is not None else None,
            "breached": row.breached,
        }
        for row in rows
    ]


async def staff_breaches(
    session: AsyncSession, start: datetime, end: datetime
) -> dict[int, dict[str, Any]]:
    """Просрочки за период по сотрудникам и чатам:
    {staff_id: {"count": N, "handled": M, "share": %, "chats": {название: сколько}}}.
    `handled` — обращения, которые сотрудник вёл (знаменатель доли). Считаются обе ступени,
    но обращение — ОДИН раз. Снятые просрочкой не считаются, в знаменателе остаются.
    Обращение без ответа никому не приписано.
    """
    from sqlalchemy import or_

    rows = (
        await session.execute(
            select(
                Interaction.first_reaction_staff_id,
                Interaction.substantive_staff_id,
                Interaction.sla_breached,
                Interaction.substantive_breached,
                dismissed_exists().label("dismissed"),
                Chat.title,
                Interaction.opened_at,
                Interaction.first_reaction_at,
                Interaction.handoff_at,
                Interaction.substantive_at,
                Interaction.ttfr_business_seconds,
            )
            .join(Chat, Chat.id == Interaction.chat_id)
            .where(Interaction.chat_id.in_(_observed_chats()))
            .where(Interaction.opened_at >= start, Interaction.opened_at < end)
            .where(
                or_(
                    Interaction.first_reaction_staff_id.isnot(None),
                    Interaction.substantive_staff_id.isnot(None),
                )
            )
        )
    ).all()

    result: dict[int, dict[str, Any]] = {}
    for (
        reaction_staff, substantive_staff, sla_breached, substantive_breached, dismissed, title,
        opened_at, reacted_at, handoff_at, answered_at, ttfr_business,
    ) in rows:
        involved = {sid for sid in (reaction_staff, substantive_staff) if sid is not None}
        blamed: set[int] = set()
        if not dismissed:
            if sla_breached and reaction_staff is not None:
                blamed.add(reaction_staff)
            if substantive_breached and substantive_staff is not None:
                blamed.add(substantive_staff)
        for staff_id in involved:
            entry = result.setdefault(
                staff_id,
                {
                    "count": 0, "handled": 0, "share": 0,
                    "reaction": 0, "specialist": 0, "chats": {}, "items": [],
                },
            )
            entry["handled"] += 1
            if staff_id in blamed:
                entry["count"] += 1
                name = title or "?"
                entry["chats"][name] = entry["chats"].get(name, 0) + 1
                # Разбивка по ступеням и сами случаи — для детализации в HTML.
                if sla_breached and reaction_staff == staff_id:
                    entry["reaction"] += 1
                    entry["items"].append(
                        {
                            "kind": "reaction", "title": name, "opened_at": opened_at,
                            "since": opened_at, "until": reacted_at,
                            "business_delay": ttfr_business,
                        }
                    )
                if substantive_breached and substantive_staff == staff_id:
                    entry["specialist"] += 1
                    entry["items"].append(
                        {
                            "kind": "answer", "title": name, "opened_at": opened_at,
                            "since": handoff_at, "until": answered_at,
                            "business_delay": None,
                        }
                    )
    for entry in result.values():
        entry["share"] = round(entry["count"] * 100 / entry["handled"]) if entry["handled"] else 0
    return result


async def problem_chats(
    session: AsyncSession, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Чаты, где чаще всего не успевают ответить."""
    rows = (
        await session.execute(
            select(
                Chat.id,
                Chat.title,
                func.count().label("episodes"),
                func.count().filter(_real_breach()).label("breached"),
                # Снятое решением «не нарушение» ожиданием не считается.
                func.count().filter(live_waiting()).label("waiting"),
                func.percentile_cont(0.5)
                .within_group(Interaction.ttfr_business_seconds.asc())
                .label("median"),
            )
            .join(Chat, Chat.id == Interaction.chat_id)
            .where(Interaction.chat_id.in_(_observed_chats()))
            .where(Interaction.opened_at >= start, Interaction.opened_at < end)
            .group_by(Chat.id, Chat.title)
            .having(
                (func.count().filter(_real_breach()) > 0)
                | (func.count().filter(live_waiting()) > 0)
            )
            .order_by(
                func.count().filter(_real_breach()).desc(),
                func.count().filter(live_waiting()).desc(),
            )
        )
    ).all()

    return [
        {
            "chat_id": row.id,
            "title": row.title,
            "episodes": row.episodes,
            "breached": row.breached,
            "waiting": row.waiting,
            "median": int(row.median) if row.median is not None else None,
        }
        for row in rows
    ]


async def load_profile(
    session: AsyncSession, start: datetime, end: datetime, timezone_name: str
) -> dict[str, Any]:
    """Когда клиенты пишут: по часам и дням недели, в рабочем поясе."""
    local = func.timezone(timezone_name, Message.sent_at)

    async def _tally(part: str):
        slot = cast(func.extract(part, local), Float).label("slot")
        rows = (
            await session.execute(
                select(slot, func.count().label("messages"))
                .where(observed_filter())
                .where(Message.business_side == BusinessSide.CLIENT)
                .where(Message.sent_at >= start, Message.sent_at < end)
                .group_by(slot)
                .order_by(slot)
            )
        ).all()
        return {int(row.slot): row.messages for row in rows}

    by_hour = await _tally("hour")
    by_weekday = await _tally("isodow")

    return {"hours": by_hour, "weekdays": by_weekday}


async def after_hours(
    session: AsyncSession, start: datetime, end: datetime, calendar_cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """Кто из сотрудников работает вне графика — по календарю (выходные и праздники тоже).
    Проверка в Python, чтобы не дублировать правила календаря в SQL.
    """
    rows = (
        await session.execute(
            select(Message.sent_at, Staff.id, Staff.full_name)
            .join(Attribution, Attribution.message_id == Message.id)
            .join(Staff, Staff.id == Attribution.staff_id)
            .where(observed_filter())
            .where(Message.business_side == BusinessSide.COMPANY)
            .where(Message.sent_at >= start, Message.sent_at < end)
        )
    ).all()

    # «Вне графика» — по графику на момент сообщения.
    calendar = CalendarHistory(calendar_cfg)

    tally: dict[int, dict[str, Any]] = {}
    for sent_at, staff_id, full_name in rows:
        entry = tally.setdefault(
            staff_id,
            {"staff_id": staff_id, "full_name": full_name, "total": 0, "outside": 0},
        )
        entry["total"] += 1
        if _is_outside(sent_at, calendar.at(sent_at)):
            entry["outside"] += 1

    result = [item for item in tally.values() if item["outside"]]
    result.sort(key=lambda item: item["outside"], reverse=True)
    return result


def _is_outside(moment: datetime, calendar_cfg: dict[str, Any]) -> bool:
    return business_seconds(moment, moment + timedelta(seconds=1), calendar_cfg) == 0
