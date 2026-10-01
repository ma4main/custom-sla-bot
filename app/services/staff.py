"""Справочник сотрудников. Без привязки к чатам: связь «сотрудник — чат»
вычисляется из фактических сообщений.
"""

from __future__ import annotations

import re
import unicodedata

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, BotUser, Staff, StaffAlias
from app.services.access import Perm, require_perm
from app.services.staff_roles import ROLE_UNDECIDED
from app.text import esc

log = structlog.get_logger(__name__)


class StaffError(RuntimeError):
    pass


def normalize_name(value: str) -> str:
    """Приведение ФИО к сравнимому виду: регистр, ё/е, лишние пробелы, дефисы."""
    text = unicodedata.normalize("NFKC", value).strip().lower()
    text = text.replace("ё", "е")
    text = re.sub(r"[\s ]+", " ", text)
    text = re.sub(r"\s*-\s*", "-", text)
    return text


async def list_staff(
    session: AsyncSession,
    *,
    offset: int = 0,
    limit: int = 8,
    active_only: bool = False,
    query: str | None = None,
    role: str | None = None,
) -> tuple[list[Staff], int]:
    """Страница справочника и общее число подходящих записей. `query` — по части
    нормализованного имени («Пётр» = «Петр»). `role` — specialist / manager
    либо ROLE_UNDECIDED (в базе NULL).
    """
    stmt = select(Staff)
    count_stmt = select(func.count(Staff.id))
    if active_only:
        stmt = stmt.where(Staff.active.is_(True))
        count_stmt = count_stmt.where(Staff.active.is_(True))
    if role is not None:
        condition = Staff.role.is_(None) if role == ROLE_UNDECIDED else Staff.role == role
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    needle = normalize_name(query or "")
    if needle:
        # escape="\\": «%» и «_» во вводе означают сами себя.
        pattern = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        condition = Staff.normalized_name.like(pattern, escape="\\")
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    total = await session.scalar(count_stmt) or 0
    rows = (
        await session.scalars(stmt.order_by(Staff.full_name).offset(offset).limit(limit))
    ).all()
    return list(rows), total


async def create_staff(
    session: AsyncSession, actor: BotUser, full_name: str, discriminator: str | None = None
) -> Staff:
    """Завести сотрудника. Однофамильцы без различителя блокируются на входе."""
    require_perm(actor, Perm.STAFF_MANAGE)

    full_name = full_name.strip()
    if not full_name:
        raise StaffError("Пустое имя")

    normalized = normalize_name(full_name)
    clash = await session.scalar(
        select(Staff).where(
            Staff.normalized_name == normalized,
            Staff.discriminator.is_(None) if discriminator is None else Staff.discriminator == discriminator,
        )
    )
    if clash is not None:
        raise StaffError(
            f"Сотрудник «{clash.full_name}» уже есть. "
            "Чтобы завести однофамильца, добавьте отличитель — отчество или внутренний код."
        )

    staff = Staff(
        full_name=full_name,
        normalized_name=normalized,
        discriminator=discriminator,
    )
    session.add(staff)
    await session.flush()

    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="staff.created",
            object_type="staff",
            object_id=str(staff.id),
            payload={"full_name": full_name},
        )
    )
    log.info("staff.created", staff_id=staff.id, full_name=full_name)
    return staff


async def set_active(session: AsyncSession, actor: BotUser, staff: Staff, active: bool) -> None:
    require_perm(actor, Perm.STAFF_MANAGE)
    staff.active = active
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="staff.activated" if active else "staff.deactivated",
            object_type="staff",
            object_id=str(staff.id),
        )
    )


async def add_alias(session: AsyncSession, actor: BotUser, staff: Staff, alias: str) -> None:
    """Добавить вариант написания. Алиас сразу срабатывает на всех похожих сообщениях."""
    require_perm(actor, Perm.STAFF_MANAGE)
    normalized = normalize_name(alias)
    if not normalized:
        raise StaffError("Пустой вариант написания")
    if any(row.alias == normalized for row in staff.aliases):
        return

    # Проверка — ради понятной ошибки с именем владельца и случая «алиас совпал с ФИО
    # другого сотрудника». Уникальность алиаса держит индекс staff_alias.alias.
    owner = await session.scalar(
        select(Staff)
        .outerjoin(StaffAlias, StaffAlias.staff_id == Staff.id)
        .where(Staff.id != staff.id)
        .where(
            or_(
                Staff.normalized_name == normalized,
                StaffAlias.alias == normalized,
            )
        )
        .limit(1)
    )
    if owner is not None:
        raise StaffError(
            f"Такое написание уже закреплено за сотрудником «{owner.full_name}» — "
            "иначе автор сообщений определялся бы случайно"
        )

    staff.aliases.append(StaffAlias(staff_id=staff.id, alias=normalized))
    try:
        # SAVEPOINT: гонка даёт ту же понятную ошибку, а не падение всей транзакции.
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        staff.aliases = [row for row in staff.aliases if row.alias != normalized]
        raise StaffError(
            "Такое написание только что закрепили за другим сотрудником — "
            "иначе автор сообщений определялся бы случайно"
        ) from None

    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="staff.alias_added",
            object_type="staff",
            object_id=str(staff.id),
            payload={"alias": normalized},
        )
    )


async def unresolved_count(session: AsyncSession) -> int:
    """Сколько сообщений ждёт решения человека; условие — в attribution.py.
    Импорт локальный: attribution.py берёт отсюда normalize_name.
    """
    from app.services.attribution import count_unresolved

    return await count_unresolved(session)


# ═══════════════════════════════════════════════════════════════
# Напоминание о привязке: новый сотрудник ↔ учётки без привязки
# ═══════════════════════════════════════════════════════════════

# Память напоминалки в setting (максимальный рассмотренный staff.id), не настройка.
NUDGE_STATE_KEY = "staff_runtime"


async def _nudge_seen(session: AsyncSession) -> int:
    from app.db.models import Setting

    stored = await session.get(Setting, NUDGE_STATE_KEY)
    if stored is not None and isinstance(stored.value, dict):
        return int(stored.value.get("link_nudge_seen") or 0)
    return 0


async def _nudge_mark(session: AsyncSession, max_id: int) -> None:
    from app.db.models import Setting

    stored = await session.get(Setting, NUDGE_STATE_KEY)
    if stored is None:
        session.add(Setting(key=NUDGE_STATE_KEY, value={"link_nudge_seen": max_id}))
    else:
        stored.value = {**(stored.value or {}), "link_nudge_seen": max_id}


async def collect_link_nudge(session: AsyncSession) -> tuple[str, list[int]] | None:
    """Собрать напоминание о привязке новых сотрудников к учёткам; None — не о чем.
    Функция бронирует отметку, отправка — снаружи, вне транзакции: сбой после брони
    теряет одно напоминание, а не дублирует его.
    """
    from app.db.models import BotRole, BotUser, BotUserState

    seen = await _nudge_seen(session)
    fresh = (
        await session.scalars(
            select(Staff).where(Staff.id > seen).order_by(Staff.id)
        )
    ).all()
    if not fresh:
        return None

    # Отметка сдвигается в ЛЮБОМ случае, чтобы новички не копились.
    await _nudge_mark(session, max(person.id for person in fresh))

    unlinked = (
        await session.scalars(
            select(BotUser)
            .where(BotUser.state == BotUserState.ACTIVE)
            .where(BotUser.staff_id.is_(None))
            # Только роль «Сотрудник»: владельцу и админам привязка не обязательна.
            .where(BotUser.role == BotRole.MANAGER)
        )
    ).all()
    if not unlinked:
        return None

    from app.services.access import Perm, notification_recipients

    recipients = [
        person.tg_user_id for person in await notification_recipients(session, Perm.USER_MANAGE)
    ]
    if not recipients:
        return None

    # Имена — из Telegram и справочника: без экранирования «<» ломает HTML-разметку.
    new_names = "\n".join(f"• {esc(person.full_name)}" for person in fresh[:10])
    accounts = "\n".join(
        f"• {esc(person.display_name or person.username or person.tg_user_id)}"
        for person in unlinked[:10]
    )
    text = (
        "🔗 <b>Появились новые сотрудники в справочнике</b>\n\n"
        f"{new_names}\n\n"
        "А эти учётки в боте пока не привязаны к сотрудникам:\n"
        f"{accounts}\n\n"
        "Если это те же люди — свяжите их: «Пользователи бота» → карточка → "
        "«🔗 Привязать к сотруднику». Без привязки не работают "
        "«Мои показатели» и личные алерты."
    )
    return text, recipients


async def maybe_send_link_nudge(bot) -> bool:
    from app.db.base import session_scope

    async with session_scope() as session:
        payload = await collect_link_nudge(session)
    if payload is None:
        return False

    text, recipients = payload
    delivered = 0
    for tg_user_id in recipients:
        try:
            await bot.send_message(tg_user_id, text, parse_mode="HTML")
            delivered += 1
        except Exception:  # noqa: BLE001 — недоставленное не роняет тик
            log.exception("staff.link_nudge_failed", tg_user_id=tg_user_id)
    if delivered:
        log.info("staff.link_nudge_sent", delivered=delivered)
    return delivered > 0
