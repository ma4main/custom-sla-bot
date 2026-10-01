"""Роли, права и регистрация пользователей (docs/SCREENS.md, раздел 1).

Права хранятся набором, роль — пресет. Инварианты держатся здесь, а не в интерфейсе:
активный владелец всегда есть хотя бы один; /start сам по себе не даёт ничего.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AuditLog, BotRole, BotUser, BotUserState, InviteCode

log = structlog.get_logger(__name__)


class AccessError(RuntimeError):
    """Действие запрещено правилами доступа."""


# ═══════════════════════════════════════════════════════════════
# Права
# ═══════════════════════════════════════════════════════════════


class Perm:
    REPORT_ALL_CHATS = "report.all_chats"
    REPORT_CHAT = "report.chat"
    REPORT_ANY_STAFF = "report.any_staff"
    REPORT_SELF = "report.self"
    REPORT_EXPORT = "report.export"

    ALERT_CONFIGURE = "alert.configure"
    ALERT_RECEIVE_ALL = "alert.receive_all"
    ALERT_RECEIVE_SELF = "alert.receive_self"

    USER_MANAGE = "user.manage"
    OWNERSHIP_TRANSFER = "ownership.transfer"
    CHAT_MANAGE = "chat.manage"
    STAFF_MANAGE = "staff.manage"

    ATTRIBUTION_ASSIGN = "attribution.assign"

    SYSTEM_HEALTH = "system.health"
    SYSTEM_SETTINGS = "system.settings"


# Администратор = владелец минус владение: не может только раздавать роль владельца.
_ADMIN_PERMS = frozenset(
    {
        Perm.REPORT_ALL_CHATS,
        Perm.REPORT_CHAT,
        Perm.REPORT_ANY_STAFF,
        Perm.REPORT_SELF,
        Perm.REPORT_EXPORT,
        Perm.ALERT_CONFIGURE,
        Perm.ALERT_RECEIVE_ALL,
        Perm.ALERT_RECEIVE_SELF,
        Perm.CHAT_MANAGE,
        Perm.STAFF_MANAGE,
        Perm.USER_MANAGE,
        Perm.ATTRIBUTION_ASSIGN,
        Perm.SYSTEM_HEALTH,
        Perm.SYSTEM_SETTINGS,
    }
)

_OWNER_PERMS = _ADMIN_PERMS | {Perm.OWNERSHIP_TRANSFER}

_MANAGER_PERMS = frozenset({Perm.REPORT_SELF, Perm.ALERT_RECEIVE_SELF})

ROLE_PRESETS: dict[BotRole, frozenset[str]] = {
    BotRole.OWNER: frozenset(_OWNER_PERMS),
    BotRole.ADMIN: _ADMIN_PERMS,
    BotRole.MANAGER: _MANAGER_PERMS,
}

# Роли по-русски. MANAGER — «сотрудник», не «менеджер»: «менеджер» в разделе
# «Сотрудники» — наблюдаемая роль (тот, кто передаёт специалисту).
ROLE_LABELS: dict[BotRole, str] = {
    BotRole.OWNER: "Владелец",
    BotRole.ADMIN: "Администратор",
    BotRole.MANAGER: "Сотрудник",
}

# Что доступно роли — в приветствии и на кнопках выдачи роли.
ROLE_HINTS: dict[BotRole, str] = {
    BotRole.OWNER: "полный доступ: отчёты, чаты, сотрудники, пользователи, настройки, "
    "алерты",
    BotRole.ADMIN: "всё, кроме передачи владения: отчёты, чаты, сотрудники, "
    "пользователи, настройки, алерты",
    BotRole.MANAGER: "свои показатели и алерты по своим передачам, без доступа к чужим",
}


STATE_LABELS: dict[BotUserState, str] = {
    BotUserState.ACTIVE: "активен",
    BotUserState.DISABLED: "отключён",
    BotUserState.PENDING: "ждёт подтверждения",
}


def role_label(role: BotRole) -> str:
    return ROLE_LABELS.get(role, role.value)


def state_label(state: BotUserState) -> str:
    return STATE_LABELS.get(state, state.value)


def effective_permissions(user: BotUser) -> frozenset[str]:
    if user.state is not BotUserState.ACTIVE:
        return frozenset()

    perms = set(ROLE_PRESETS.get(user.role, frozenset()))
    overrides = user.permissions or {}
    for perm in overrides.get("grant", []):
        perms.add(perm)
    for perm in overrides.get("revoke", []):
        perms.discard(perm)
    return frozenset(perms)


def has_perm(user: BotUser | None, perm: str) -> bool:
    return user is not None and perm in effective_permissions(user)


def require_perm(user: BotUser | None, perm: str) -> None:
    if not has_perm(user, perm):
        # Ключ права — в лог; пользователю — общий текст.
        log.info("access.denied", perm=perm, user_id=user.id if user is not None else None)
        raise AccessError("Недостаточно прав для этого действия")


# ═══════════════════════════════════════════════════════════════
# Регистрация
# ═══════════════════════════════════════════════════════════════


async def count_active_owners(session: AsyncSession, exclude_id: int | None = None) -> int:
    stmt = select(func.count(BotUser.id)).where(
        BotUser.role == BotRole.OWNER, BotUser.state == BotUserState.ACTIVE
    )
    if exclude_id is not None:
        stmt = stmt.where(BotUser.id != exclude_id)
    return await session.scalar(stmt) or 0


async def get_user(session: AsyncSession, tg_user_id: int) -> BotUser | None:
    return await session.scalar(select(BotUser).where(BotUser.tg_user_id == tg_user_id))


async def notification_recipients(session: AsyncSession, perm: str) -> list[BotUser]:
    """Кому уходит уведомление — по праву, а не по названию роли. Фильтр в Python:
    право складывается из роли и личных поправок в JSONB.
    """
    everyone = (
        await session.scalars(select(BotUser).where(BotUser.state == BotUserState.ACTIVE))
    ).all()
    return [user for user in everyone if has_perm(user, perm)]


async def personal_alert_recipients(session: AsyncSession) -> list[BotUser]:
    """Лички для алертов и рассылок: по праву И с включённой личкой. Заявки на доступ
    и сбой ИИ идут через notification_recipients и флага не видят.
    """
    return [
        user
        for user in await notification_recipients(session, Perm.ALERT_RECEIVE_ALL)
        if user.notify_personal
    ]


def personal_mute_blocker(
    alert_cfg: dict, digest_cfg: dict, notify_group_id: int | None
) -> str | None:
    """Почему НЕЛЬЗЯ выключить личку сейчас; None — можно. Выключить можно, только когда
    алерты и рассылки уже идут в группу.
    """
    if notify_group_id is None:
        return (
            "Группа уведомлений не задана — выключив личные сообщения, вы не получите "
            "ничего. Сначала нужна группа."
        )
    if not bool(alert_cfg.get("to_group")):
        return (
            "Сначала включите «Алерты в группу уведомлений» (Настройки → "
            "Алерты) — иначе алерты некуда будет слать."
        )
    if not bool(digest_cfg.get("to_group")):
        return (
            "Сначала включите «Слать и в группу уведомлений» (Настройки → "
            "Отчёты по расписанию → 📤 Общее для отчётов) — иначе отчёты "
            "некуда будет слать."
        )
    return None


async def register_start(
    session: AsyncSession,
    tg_user_id: int,
    *,
    username: str | None = None,
    display_name: str | None = None,
    invite_code: str | None = None,
) -> BotUser:
    """Обработать /start; запись пользователя возвращается всегда. Активным он становится,
    если он назначенный владелец, погасил код приглашения или подтверждён владельцем.
    """
    settings = get_settings()
    user = await get_user(session, tg_user_id)

    if user is not None:
        if username and user.username != username:
            user.username = username
        # Код приглашения активирует и того, кто уже в базе как pending.
        if invite_code and user.state is BotUserState.PENDING:
            await _apply_invite(session, user, invite_code)
        return user

    user = BotUser(
        tg_user_id=tg_user_id,
        username=username,
        display_name=display_name,
        role=BotRole.MANAGER,
        state=BotUserState.PENDING,
    )

    # Владелец назначается по Telegram ID из конфигурации:
    # чужой аккаунт, зашедший раньше, просто уходит в заявки.
    if settings.bootstrap_owner_tg_id and tg_user_id == settings.bootstrap_owner_tg_id:
        user.role = BotRole.OWNER
        user.state = BotUserState.ACTIVE
        session.add(user)
        await session.flush()
        session.add(
            AuditLog(
                actor_user_id=user.id,
                action="owner.bootstrap",
                object_type="bot_user",
                object_id=str(user.id),
            )
        )
        log.info("owner.bootstrap", tg_user_id=tg_user_id)
        return user

    session.add(user)
    await session.flush()

    if invite_code:
        await _apply_invite(session, user, invite_code)

    return user


async def _apply_invite(session: AsyncSession, user: BotUser, code: str) -> bool:
    """Погасить код атомарно: одноразовость держит условный UPDATE, а не SELECT+UPDATE."""
    from sqlalchemy import or_, update

    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(InviteCode)
        .where(
            InviteCode.code_hash == hash_invite_code(code),
            InviteCode.used_at.is_(None),
            or_(InviteCode.expires_at.is_(None), InviteCode.expires_at > now),
            # Только роли, которые разрешено выдавать из интерфейса.
            InviteCode.role.in_(list(ASSIGNABLE_ROLES)),
        )
        .values(used_by=user.id, used_at=now)
        .returning(InviteCode.role, InviteCode.created_by)
    )
    row = result.first()
    if row is None:
        return False

    user.role = row.role
    user.state = BotUserState.ACTIVE
    user.invited_by = row.created_by

    session.add(
        AuditLog(
            actor_user_id=row.created_by,
            action="user.activated_by_invite",
            object_type="bot_user",
            object_id=str(user.id),
            payload={"role": row.role.value},
        )
    )
    log.info("user.invited", tg_user_id=user.tg_user_id, role=row.role.value)
    return True


# Роли, которые можно выдать из интерфейса. Владелец — только передачей/совладением.
# Список общий для всех путей выдачи.
# Порядок показа — от больших прав к меньшим, кнопки не должны переставляться.
ASSIGNABLE_ORDER: tuple[BotRole, ...] = (BotRole.ADMIN, BotRole.MANAGER)
ASSIGNABLE_ROLES = frozenset(ASSIGNABLE_ORDER)


def _guard_assignable(role: BotRole) -> None:
    if role in ASSIGNABLE_ROLES:
        return
    if role is BotRole.OWNER:
        raise AccessError("Роль владельца не выдаётся напрямую — только передачей роли")
    raise AccessError("Эту роль нельзя выдать из бота")


def hash_invite_code(code: str) -> str:
    """Хеш кода приглашения — то единственное, что попадает в базу."""
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


# Срок жизни приглашения: утёкшая ссылка не должна работать бессрочно.
INVITE_TTL = timedelta(hours=48)


async def create_invite(
    session: AsyncSession, actor: BotUser, role: BotRole, expires_at: datetime | None = None
) -> tuple[InviteCode, str]:
    """Создать приглашение; возвращает (запись, КОД). Код нигде не сохраняется —
    в базе только хеш, потерянная ссылка выпускается заново. Без срока действует INVITE_TTL.
    """
    require_perm(actor, Perm.USER_MANAGE)
    _guard_assignable(role)

    code = secrets.token_urlsafe(16)
    invite = InviteCode(
        code_hash=hash_invite_code(code),
        role=role,
        created_by=actor.id,
        expires_at=expires_at or datetime.now(timezone.utc) + INVITE_TTL,
    )
    session.add(invite)
    return invite, code


async def approve_user(
    session: AsyncSession,
    actor: BotUser,
    user: BotUser,
    role: BotRole,
    staff_id: int | None = None,
) -> None:
    require_perm(actor, Perm.USER_MANAGE)
    _guard_assignable(role)

    user.role = role
    user.state = BotUserState.ACTIVE
    user.staff_id = staff_id
    user.invited_by = actor.id

    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="user.approved",
            object_type="bot_user",
            object_id=str(user.id),
            payload={"role": role.value, "staff_id": staff_id},
        )
    )
    log.info("user.approved", tg_user_id=user.tg_user_id, role=role.value)


def _guard_owner_target(actor: BotUser, target: BotUser) -> None:
    """Владельца понижает или отключает только другой владелец (админ не может
    перехватить бота).
    """
    if target.role is BotRole.OWNER and actor.role is not BotRole.OWNER:
        raise AccessError("Менять владельца может только другой владелец")


# Ключ транзакционной advisory-блокировки проверки «последний владелец».
OWNER_INVARIANT_LOCK_KEY = 738102


async def _lock_owner_invariant(session: AsyncSession) -> None:
    """Сериализовать проверку «последний владелец»: проверка и запись — разные шаги,
    и без транзакционной advisory-блокировки две параллельные операции над двумя
    владельцами вместе оставили бы систему без владельца.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": OWNER_INVARIANT_LOCK_KEY}
    )


async def change_role(
    session: AsyncSession, actor: BotUser, user: BotUser, role: BotRole
) -> None:
    require_perm(actor, Perm.USER_MANAGE)

    _guard_assignable(role)
    _guard_owner_target(actor, user)

    await _lock_owner_invariant(session)
    if user.role is BotRole.OWNER and await count_active_owners(session, exclude_id=user.id) == 0:
        raise AccessError(
            "Это единственный владелец. Сначала передайте роль другому пользователю."
        )

    previous = user.role
    user.role = role
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="user.role_changed",
            object_type="bot_user",
            object_id=str(user.id),
            payload={"from": previous.value, "to": role.value},
        )
    )


async def disable_user(session: AsyncSession, actor: BotUser, user: BotUser) -> None:
    require_perm(actor, Perm.USER_MANAGE)
    _guard_owner_target(actor, user)

    await _lock_owner_invariant(session)
    if user.role is BotRole.OWNER and await count_active_owners(session, exclude_id=user.id) == 0:
        raise AccessError("Нельзя отключить единственного владельца")

    user.state = BotUserState.DISABLED
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="user.disabled",
            object_type="bot_user",
            object_id=str(user.id),
        )
    )
    log.info("user.disabled", tg_user_id=user.tg_user_id)


async def enable_user(session: AsyncSession, actor: BotUser, user: BotUser) -> None:
    """Вернуть отключённого пользователя с прежней ролью. Права — как у отключения:
    владельца возвращает только владелец. Заявки сюда не относятся — их подтверждают
    из очереди с выбором роли."""
    require_perm(actor, Perm.USER_MANAGE)
    _guard_owner_target(actor, user)
    if user.state is not BotUserState.DISABLED:
        raise AccessError("Пользователь не отключён")

    user.state = BotUserState.ACTIVE
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="user.enabled",
            object_type="bot_user",
            object_id=str(user.id),
            payload={"role": user.role.value},
        )
    )
    log.info("user.enabled", tg_user_id=user.tg_user_id, role=user.role.value)


async def promote_to_owner(session: AsyncSession, actor: BotUser, target: BotUser) -> None:
    """Сделать пользователя совладельцем. Владельцев может быть несколько;
    инвариант — их не может стать ноль.
    """
    require_perm(actor, Perm.OWNERSHIP_TRANSFER)

    if target.state is not BotUserState.ACTIVE:
        raise AccessError("Совладельцем можно сделать только активного пользователя")
    if target.role is BotRole.OWNER:
        raise AccessError("Этот пользователь уже владелец")

    previous = target.role
    target.role = BotRole.OWNER

    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="ownership.co_owner_added",
            object_type="bot_user",
            object_id=str(target.id),
            payload={"from": previous.value, "to_tg_id": target.tg_user_id},
        )
    )
    log.info("ownership.co_owner_added", actor=actor.tg_user_id, target=target.tg_user_id)


async def transfer_ownership(session: AsyncSession, actor: BotUser, target: BotUser) -> None:
    """Передать роль владельца; прежний становится админом. Это механизм восстановления
    доступа без разработчика и правки базы.
    """
    require_perm(actor, Perm.OWNERSHIP_TRANSFER)

    if target.id == actor.id:
        raise AccessError("Нельзя передать роль самому себе")
    if target.state is not BotUserState.ACTIVE:
        raise AccessError("Передать роль можно только активному пользователю")

    actor.role = BotRole.ADMIN
    target.role = BotRole.OWNER

    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="ownership.transferred",
            object_type="bot_user",
            object_id=str(target.id),
            payload={"from_tg_id": actor.tg_user_id, "to_tg_id": target.tg_user_id},
        )
    )
    log.info("ownership.transferred", from_id=actor.tg_user_id, to_id=target.tg_user_id)


async def link_staff(
    session: AsyncSession, actor: BotUser, target: BotUser, staff_id: int | None
) -> None:
    """Связать учётную запись бота с сотрудником из справочника: персональные отчёты
    считаются по атрибуции, которая ведёт на Staff.
    """
    require_perm(actor, Perm.USER_MANAGE)

    if staff_id is not None:
        from app.db.models import Staff

        person = await session.get(Staff, staff_id)
        if person is None:
            raise AccessError("Сотрудник не найден")
        if not person.active:
            raise AccessError("Нельзя привязать к деактивированному сотруднику")

        # Один сотрудник — одна учётная запись.
        busy = await session.scalar(
            select(BotUser)
            .where(BotUser.staff_id == staff_id)
            .where(BotUser.id != target.id)
            .limit(1)
        )
        if busy is not None:
            name = busy.display_name or busy.username or str(busy.tg_user_id)
            raise AccessError(f"Этот сотрудник уже привязан к учётной записи «{name}»")

    previous = target.staff_id
    target.staff_id = staff_id

    if staff_id is not None:
        try:
            # SAVEPOINT: гонка двух привязок даёт ту же понятную ошибку, а не падение
            # транзакции. Инвариант держит частичный индекс uq_bot_user_staff.
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            target.staff_id = previous
            raise AccessError(
                "Этого сотрудника только что привязали к другой учётной записи"
            ) from None

    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="user.staff_linked",
            object_type="bot_user",
            object_id=str(target.id),
            payload={"from": previous, "to": staff_id},
        )
    )
    log.info("user.staff_linked", tg_user_id=target.tg_user_id, staff_id=staff_id)

    if staff_id is not None:
        # Привязка = «этот Telegram-аккаунт — сотрудник»: его прямые сообщения задним
        # числом становятся стороной компании. Сторона решается тем же вызовом, что
        # на приёме: решение человека про отправителя (`sender_rule`) сильнее привязки.
        from app.db.models import BusinessSide, Message, TransportActorKind
        from app.services.ingestion import SIDE_RULE_VERSION, resolve_business_side
        from app.services.reprocess import invalidate_side_change

        candidates = (
            await session.scalars(
                select(Message)
                .where(Message.tg_user_id == target.tg_user_id)
                .where(Message.transport_actor_kind == TransportActorKind.HUMAN_USER)
                .where(Message.business_side == BusinessSide.CLIENT)
            )
        ).all()
        flipped: list[int] = []
        reclassify: list[int] = []
        for message in candidates:
            side = await resolve_business_side(
                session,
                message.transport_actor_kind,
                message.tg_user_id,
                message.text,
                chat_id=message.chat_id,
            )
            if side is BusinessSide.CLIENT:
                continue
            message.business_side = side
            message.side_rule_version = SIDE_RULE_VERSION
            flipped.append(message.id)
            if side is BusinessSide.COMPANY:
                # Клиентский вердикт на стороне компании бессмыслен — переспрос промптом компании.
                message.needs_reclassification = True
                reclassify.append(message.id)
        await invalidate_side_change(session, reclassify, [])
        if flipped:
            log.info(
                "user.staff_linked.sides_flipped",
                tg_user_id=target.tg_user_id,
                messages=len(flipped),
            )

        # Сразу же авторство: привязка — путь атрибуции прямых сообщений, старые
        # сообщения подписываются здесь, а не после перезапуска воркера.
        from app.services.attribution import attribute_all

        marked = await attribute_all(session)
        log.info(
            "user.staff_linked.attributed",
            tg_user_id=target.tg_user_id,
            resolved=marked["resolved"],
            unresolved=marked["unresolved"],
        )
