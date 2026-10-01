"""Показатели нагрузки и скорости для отчётов.

Выборки — только по интервалам наблюдения за чатами и только внутри периода.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.attribution import unresolved_condition
from app.services.ingestion import ANONYMOUS_ADMIN_BOT_ID
from app.services.tracking import observed_filter
from app.db.models import (
    Attribution,
    BusinessSide,
    Chat,
    Message,
    Staff,
)


def _observed(query):
    """Сообщения из интервалов наблюдения, а не по текущему состоянию чата:
    пауза сегодня не должна менять отчёт за прошлый период."""
    return query.where(observed_filter())


def _local_day():
    """Календарный день в рабочем поясе, а не в UTC."""
    from app.config import get_settings

    return func.date(func.timezone(get_settings().tz, Message.sent_at))


def _period(query, start: datetime, end: datetime):
    return _observed(query.where(Message.sent_at >= start, Message.sent_at < end))


async def load_summary(session: AsyncSession, start: datetime, end: datetime) -> dict[str, Any]:
    """Сводка по всем отслеживаемым чатам: тоталы, разбивки, качество данных."""
    from app.db.models import ChatTrackingPeriod

    # Чаты, наблюдавшиеся в периоде (по интервалам), а не текущее состояние.
    period = ChatTrackingPeriod
    tracked = await session.scalar(
        select(func.count(func.distinct(period.chat_id))).where(
            period.started_at < end,
            (period.ended_at.is_(None)) | (period.ended_at > start),
        )
    ) or 0

    base = _period(
        select(func.count(Message.id)), start, end
    )
    incoming = await session.scalar(
        base.where(Message.business_side == BusinessSide.CLIENT)
    ) or 0
    outgoing = await session.scalar(
        base.where(Message.business_side == BusinessSide.COMPANY)
    ) or 0

    per_chat_rows = await session.execute(
        _period(
            select(
                Chat.id,
                Chat.title,
                func.count(Message.id)
                .filter(Message.business_side == BusinessSide.CLIENT)
                .label("incoming"),
                func.count(Message.id)
                .filter(Message.business_side == BusinessSide.COMPANY)
                .label("outgoing"),
                func.coalesce(
                    func.sum(Message.char_count).filter(
                        Message.business_side == BusinessSide.CLIENT
                    ),
                    0,
                ).label("client_chars"),
                func.coalesce(
                    func.sum(Message.char_count).filter(
                        Message.business_side == BusinessSide.COMPANY
                    ),
                    0,
                ).label("company_chars"),
                func.max(Message.sent_at).label("last_activity"),
            )
            .join(Message, Message.chat_id == Chat.id),
            start,
            end,
        ).group_by(Chat.id, Chat.title)
    )
    chats = [dict(row._mapping) for row in per_chat_rows.all()]
    chats.sort(key=lambda item: item["incoming"] + item["outgoing"], reverse=True)

    per_staff_rows = await session.execute(
        _period(
            select(
                Staff.id,
                Staff.full_name,
                func.count(Message.id).label("messages"),
                func.coalesce(func.sum(Message.char_count), 0).label("chars"),
                func.count(func.distinct(Message.chat_id)).label("chats_touched"),
                func.count(func.distinct(_local_day())).label("active_days"),
            )
            .join(Attribution, Attribution.staff_id == Staff.id)
            .join(Message, Message.id == Attribution.message_id),
            start,
            end,
        ).group_by(Staff.id, Staff.full_name)
    )
    staff = [dict(row._mapping) for row in per_staff_rows.all()]
    staff.sort(key=lambda item: item["messages"], reverse=True)

    # Качество данных: доля исходящих без определённого автора.
    unresolved = await session.scalar(
        _period(
            select(func.count(Message.id))
            .join(Attribution, Attribution.message_id == Message.id)
            # Подписи с решением «это не сотрудник» сюда не входят.
            .where(unresolved_condition()),
            start,
            end,
        )
    ) or 0
    # Сообщения «от имени группы» (анонимный админ) автора не имеют по построению
    # и в очередь разметки не попадают — качеством данных не считаются.
    unattributed = await session.scalar(
        _period(
            select(func.count(Message.id))
            .outerjoin(Attribution, Attribution.message_id == Message.id)
            .where(Message.business_side == BusinessSide.COMPANY)
            .where(Attribution.message_id.is_(None))
            .where(
                or_(
                    Message.tg_user_id.is_(None),
                    Message.tg_user_id != ANONYMOUS_ADMIN_BOT_ID,
                )
            ),
            start,
            end,
        )
    ) or 0

    unresolved_total = unresolved + unattributed
    unresolved_pct = round(unresolved_total * 100 / outgoing, 1) if outgoing else 0.0

    return {
        "tracked": tracked,
        "incoming": incoming,
        "outgoing": outgoing,
        "chats": chats,
        "staff": staff,
        "unresolved": unresolved_total,
        "unresolved_pct": unresolved_pct,
    }


async def load_chat_report(
    session: AsyncSession, chat_id: int, start: datetime, end: datetime
) -> dict[str, Any] | None:
    """Один чат: тоталы и кто из сотрудников в нём отвечал."""
    chat = await session.get(Chat, chat_id)
    if chat is None:
        return None

    base = _period(select(func.count(Message.id)).where(Message.chat_id == chat_id), start, end)
    incoming = await session.scalar(
        base.where(Message.business_side == BusinessSide.CLIENT)
    ) or 0
    outgoing = await session.scalar(
        base.where(Message.business_side == BusinessSide.COMPANY)
    ) or 0
    client_chars = await session.scalar(
        _period(
            select(func.coalesce(func.sum(Message.char_count), 0))
            .where(Message.chat_id == chat_id)
            .where(Message.business_side == BusinessSide.CLIENT),
            start,
            end,
        )
    ) or 0
    company_chars = await session.scalar(
        _period(
            select(func.coalesce(func.sum(Message.char_count), 0))
            .where(Message.chat_id == chat_id)
            .where(Message.business_side == BusinessSide.COMPANY),
            start,
            end,
        )
    ) or 0

    per_staff_rows = await session.execute(
        _period(
            select(
                Staff.full_name,
                func.count(Message.id).label("messages"),
                func.coalesce(func.sum(Message.char_count), 0).label("chars"),
            )
            .join(Attribution, Attribution.staff_id == Staff.id)
            .join(Message, Message.id == Attribution.message_id)
            .where(Message.chat_id == chat_id),
            start,
            end,
        ).group_by(Staff.id, Staff.full_name)
    )
    staff = sorted(
        (dict(row._mapping) for row in per_staff_rows.all()),
        key=lambda item: item["messages"],
        reverse=True,
    )

    return {
        "title": chat.title or str(chat.tg_chat_id),
        "state": chat.state.value,
        "tracked_since": chat.tracked_since,
        "incoming": incoming,
        "outgoing": outgoing,
        "client_chars": client_chars,
        "company_chars": company_chars,
        "staff": staff,
    }


async def load_staff_report(
    session: AsyncSession, staff_id: int, start: datetime, end: datetime
) -> dict[str, Any] | None:
    """Один сотрудник: сколько сообщений и в каких чатах."""
    person = await session.get(Staff, staff_id)
    if person is None:
        return None

    per_chat_rows = await session.execute(
        _period(
            select(
                Chat.title,
                func.count(Message.id).label("messages"),
                func.coalesce(func.sum(Message.char_count), 0).label("chars"),
                func.max(Message.sent_at).label("last_activity"),
            )
            .join(Message, Message.chat_id == Chat.id)
            .join(Attribution, Attribution.message_id == Message.id)
            .where(Attribution.staff_id == staff_id),
            start,
            end,
        ).group_by(Chat.id, Chat.title)
    )
    chats = sorted(
        (dict(row._mapping) for row in per_chat_rows.all()),
        key=lambda item: item["messages"],
        reverse=True,
    )

    total = sum(item["messages"] for item in chats)
    chars = sum(item["chars"] for item in chats)
    active_days = await session.scalar(
        _period(
            select(func.count(func.distinct(_local_day())))
            .join(Attribution, Attribution.message_id == Message.id)
            .where(Attribution.staff_id == staff_id),
            start,
            end,
        )
    ) or 0

    # Личная скорость: по обращениям, где этот человек отреагировал первым.
    from app.db.models import Interaction

    own_reactions = (
        Interaction.first_reaction_staff_id == staff_id,
        Interaction.opened_at >= start,
        Interaction.opened_at < end,
    )
    reactions = await session.scalar(
        select(func.count(Interaction.id))
        .where(*own_reactions)
        .where(Interaction.ttfr_business_seconds.isnot(None))
    ) or 0
    answered = await session.scalar(
        select(func.count(Interaction.id))
        .where(Interaction.substantive_staff_id == staff_id)
        .where(Interaction.opened_at >= start, Interaction.opened_at < end)
    ) or 0
    from app.services.dismissals import dismissed_exists

    # Обе ступени, как в `report_lab.staff_breaches`; обращение считается один раз.
    breached = await session.scalar(
        select(func.count(Interaction.id))
        .where(
            or_(
                (Interaction.first_reaction_staff_id == staff_id)
                & Interaction.sla_breached.is_(True),
                (Interaction.substantive_staff_id == staff_id)
                & Interaction.substantive_breached.is_(True),
            )
        )
        .where(~dismissed_exists())
        .where(Interaction.opened_at >= start, Interaction.opened_at < end)
    ) or 0

    return {
        "full_name": person.full_name,
        "active": person.active,
        "messages": total,
        "chars": chars,
        "active_days": active_days,
        "chats": chats,
        "reactions": reactions,
        # Та же формула, что в сводном отчёте (`percentile`).
        "reaction_median": await percentile(
            session, Interaction.ttfr_business_seconds, 0.5, *own_reactions
        ),
        "reaction_p90": await percentile(
            session, Interaction.ttfr_business_seconds, 0.9, *own_reactions
        ),
        "answered": answered,
        "breached": breached,
    }


async def percentile(session: AsyncSession, column, fraction: float, *conditions) -> int | None:
    """Перцентиль (`percentile_cont`) — одна формула для всех отчётов."""
    query = select(func.percentile_cont(fraction).within_group(column.asc())).where(
        column.isnot(None)
    )
    for condition in conditions:
        query = query.where(condition)
    value = await session.scalar(query)
    return int(value) if value is not None else None


async def load_speed(session: AsyncSession, start: datetime, end: datetime) -> dict[str, Any]:
    """Метрики скорости по обращениям, открытым внутри периода."""
    from app.db.models import Interaction, InteractionState
    from app.services.settings_store import (
        DEFAULT_REACTION_MINUTES,
        DEFAULT_SPECIALIST_MINUTES,
        DEFAULT_WAIT_REACTION_HOURS,
        DEFAULT_WAIT_SPECIALIST_DAYS,
        get_section,
    )

    alert_cfg = await get_section(session, "alerts")
    episode_cfg = await get_section(session, "episodes")
    reaction_limit = int(alert_cfg.get("threshold_minutes") or DEFAULT_REACTION_MINUTES) * 60
    substantive_limit = int(
        alert_cfg.get("substantive_threshold_minutes") or DEFAULT_SPECIALIST_MINUTES
    ) * 60

    base = (
        select(Interaction)
        .where(Interaction.opened_at >= start, Interaction.opened_at < end)
    )
    total = await session.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0

    async def _count(*conditions) -> int:
        query = base
        for condition in conditions:
            query = query.where(condition)
        return await session.scalar(select(func.count()).select_from(query.subquery())) or 0

    # Снятое решением «не нарушение» обращение не считается ни просрочкой,
    # ни «без ответа», а уходит в «ответ не требовался»: сумма состояний
    # сходится с total.
    from app.services.dismissals import OPEN_STATES, dismissed_exists, live_waiting

    dismissed = dismissed_exists()

    answered = await _count(Interaction.state == InteractionState.ANSWERED)
    no_response = await _count(
        (Interaction.state == InteractionState.NO_RESPONSE_NEEDED)
        | (
            Interaction.state.in_(
                [InteractionState.ABANDONED, *OPEN_STATES]
            )
            & dismissed
        )
    )
    waiting = await _count(live_waiting())
    # Кнопки-срезы глушатся паузой чата, а «ждут ответа» — история периода;
    # ожидание в запаузенных чатах показывается отдельной припиской.
    from app.services.tracking import currently_tracked_chats

    waiting_paused = await _count(
        live_waiting(),
        Interaction.chat_id.notin_(currently_tracked_chats()),
    )
    # По флагам эпизода, а не пересчётом рабочего времени в SQL:
    # один источник правды с алертами.
    breach_reaction = await _count(Interaction.sla_breached.is_(True), ~dismissed)
    breach_substantive = await _count(
        Interaction.substantive_breached.is_(True), ~dismissed
    )

    async def _pct(column, fraction: float) -> int | None:
        return await percentile(
            session, column, fraction,
            Interaction.opened_at >= start, Interaction.opened_at < end,
        )

    # Закрытые по пределу: ответа не было или он не признан содержательным.
    timed_out = await _count(Interaction.state == InteractionState.ABANDONED, ~dismissed)
    # Знаменатель для «специалисты ответили в срок».
    handoffs = await _count(Interaction.handoff_at.isnot(None))

    return {
        "total": total,
        "answered": answered,
        "no_response": no_response,
        "waiting": waiting,
        "waiting_paused": waiting_paused,
        "timed_out": timed_out,
        "handoffs": handoffs,
        "breach_reaction": breach_reaction,
        "breach_substantive": breach_substantive,
        "ttfr_median": await _pct(Interaction.ttfr_business_seconds, 0.5),
        "ttfr_p90": await _pct(Interaction.ttfr_business_seconds, 0.9),
        "ttfa_median": await _pct(Interaction.ttfa_business_seconds, 0.5),
        "ttfa_p90": await _pct(Interaction.ttfa_business_seconds, 0.9),
        "reaction_limit_min": reaction_limit // 60,
        "wait_reaction_hours": int(
            episode_cfg.get("wait_reaction_hours") or DEFAULT_WAIT_REACTION_HOURS
        ),
        "wait_specialist_days": int(
            episode_cfg.get("wait_specialist_days") or DEFAULT_WAIT_SPECIALIST_DAYS
        ),
        "substantive_limit_min": substantive_limit // 60,
    }
