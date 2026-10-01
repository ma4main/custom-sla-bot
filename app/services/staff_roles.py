"""Наблюдаемые роли сотрудников: менеджер (помощник) или специалист.

Закрывает ЧУЖИЕ передачи (substantive_staff_id) → специалист; передаёт сам
(handoff_staff_id) → менеджер. Нужно MIN_SIGNALS независимых случаев. Ручную роль
автоматика не трогает; автоматическая при затишье не сбрасывается.
"""

from __future__ import annotations

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, BotUser, Interaction, Staff
from app.services.access import Perm, require_perm

log = structlog.get_logger(__name__)

ROLE_SPECIALIST = "specialist"
ROLE_MANAGER = "manager"

# Псевдороль для среза «роль не определена» (в базе NULL).
ROLE_UNDECIDED = "none"

SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"

# Сколько независимых случаев нужно, чтобы назначить роль автоматически.
MIN_SIGNALS = 2

ROLE_LABELS = {
    ROLE_SPECIALIST: "специалист",
    ROLE_MANAGER: "менеджер",
}


def role_line(person: Staff) -> str:
    if person.role not in ROLE_LABELS:
        return "пока не определена — наберётся по передачам"
    label = ROLE_LABELS[person.role]
    source = "задана вручную" if person.role_source == SOURCE_MANUAL else "по наблюдениям"
    return f"{label} ({source})"


async def observed_counts(session: AsyncSession) -> dict[int, dict[str, int]]:
    handed_rows = (
        await session.execute(
            select(Interaction.handoff_staff_id, func.count())
            .where(Interaction.handoff_staff_id.isnot(None))
            .group_by(Interaction.handoff_staff_id)
        )
    ).all()
    closed_rows = (
        await session.execute(
            select(Interaction.substantive_staff_id, func.count())
            .where(Interaction.substantive_staff_id.isnot(None))
            # Только чужие передачи: свою «передала» человек мог закрыть сам.
            .where(Interaction.handoff_staff_id.isnot(None))
            .where(Interaction.handoff_staff_id != Interaction.substantive_staff_id)
            .group_by(Interaction.substantive_staff_id)
        )
    ).all()

    counts: dict[int, dict[str, int]] = {}
    for staff_id, handed in handed_rows:
        counts.setdefault(staff_id, {"handed": 0, "closed": 0})["handed"] = handed
    for staff_id, closed in closed_rows:
        counts.setdefault(staff_id, {"handed": 0, "closed": 0})["closed"] = closed
    return counts


async def refresh_observed_roles(session: AsyncSession) -> int:
    counts = await observed_counts(session)
    people = (
        await session.scalars(select(Staff).where(Staff.active.is_(True)))
    ).all()

    changed = 0
    for person in people:
        if person.role_source == SOURCE_MANUAL:
            continue
        stats = counts.get(person.id, {"handed": 0, "closed": 0})
        handed, closed = stats["handed"], stats["closed"]

        if closed >= MIN_SIGNALS and closed >= handed:
            observed = ROLE_SPECIALIST
        elif handed >= MIN_SIGNALS and handed > closed:
            observed = ROLE_MANAGER
        else:
            continue  # сигнала мало — назначенное не трогаем

        if person.role != observed:
            person.role = observed
            person.role_source = SOURCE_AUTO
            changed += 1
            log.info(
                "staff.role_observed",
                full_name=person.full_name,
                role=observed,
                handed=handed,
                closed=closed,
            )
    return changed


async def set_manual_role(
    session: AsyncSession, actor: BotUser, person: Staff, role: str | None
) -> None:
    """Назначить роль вручную; None — вернуть автоопределение (с немедленным пересчётом)."""
    require_perm(actor, Perm.STAFF_MANAGE)
    if role not in (ROLE_SPECIALIST, ROLE_MANAGER, None):
        raise ValueError(f"Неизвестная роль: {role}")

    previous = person.role
    if role is None:
        person.role = None
        person.role_source = None
    else:
        person.role = role
        person.role_source = SOURCE_MANUAL

    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="staff.role_changed",
            object_type="staff",
            object_id=str(person.id),
            payload={"from": previous, "to": role, "manual": role is not None},
        )
    )
    if role is None:
        await refresh_observed_roles(session)
