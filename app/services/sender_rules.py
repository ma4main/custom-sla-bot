"""Разметка «кто есть кто в чате» по отправителям.

Единица разметки — ОТПРАВИТЕЛЬ: решение человека действует на всю его переписку.
Данные по правилу меняет ОДНА процедура — `apply_rule`: сторона, вердикт и атрибуция
обязаны меняться вместе.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Attribution,
    AttributionMethod,
    AuditLog,
    BotUser,
    BusinessSide,
    Chat,
    Message,
    SenderRule,
    SenderRuleKind,
    SenderRuleSide,
    Staff,
    TelegramUpdate,
    TransportActorKind,
)
from app.services.access import Perm, require_perm
from app.services.ingestion import ANONYMOUS_ADMIN_BOT_ID, strip_leading_mention
from app.services.transcript import client_numbers

log = structlog.get_logger(__name__)

# `system` → INTEGRATOR_SYSTEM: движок исключает такие сообщения из обеих сторон.
SIDE_BY_RULE: dict[SenderRuleSide, BusinessSide] = {
    SenderRuleSide.COMPANY: BusinessSide.COMPANY,
    SenderRuleSide.CLIENT: BusinessSide.CLIENT,
    SenderRuleSide.SYSTEM: BusinessSide.INTEGRATOR_SYSTEM,
}

# Транспорт, которым правило управляет. Служебные события Telegram («участник вошёл»)
# тоже несут tg_user_id, но в правило не попадают.
RULED_ACTOR_KINDS = (TransportActorKind.HUMAN_USER, TransportActorKind.OTHER_BOT)

SIDE_LABELS = {
    SenderRuleSide.COMPANY: "сотрудник (компания)",
    SenderRuleSide.CLIENT: "клиент",
    SenderRuleSide.SYSTEM: "система / бот",
}


@dataclass(frozen=True)
class RuleSnapshot:
    """Слепок правила ДО изменения: у живого объекта SQLAlchemy уже новые значения."""

    kind: SenderRuleKind
    key: int
    chat_id: int | None
    side: SenderRuleSide
    staff_id: int | None


def snapshot(rule: SenderRule | None) -> RuleSnapshot | None:
    if rule is None:
        return None
    return RuleSnapshot(rule.kind, rule.key, rule.chat_id, rule.side, rule.staff_id)


def kind_for(actor_kind: TransportActorKind, tg_user_id: int | None) -> SenderRuleKind | None:
    """Вид правила для отправителя; None — правила не бывает. Анонимный админ узнаётся
    по служебной учётке (транспорт тот же OTHER_BOT), и его правило — на один чат.
    """
    if tg_user_id is None:
        return None
    if tg_user_id == ANONYMOUS_ADMIN_BOT_ID:
        return SenderRuleKind.ANONYMOUS_ADMIN
    if actor_kind is TransportActorKind.OTHER_BOT:
        return SenderRuleKind.BOT
    if actor_kind is TransportActorKind.HUMAN_USER:
        return SenderRuleKind.TG_USER
    return None


def scope_chat_id(kind: SenderRuleKind, chat_id: int | None) -> int | None:
    """Область правила: чат — только у анонимного админа, иначе все чаты."""
    return chat_id if kind is SenderRuleKind.ANONYMOUS_ADMIN else None


async def load_rule(
    session: AsyncSession,
    kind: SenderRuleKind,
    key: int,
    chat_id: int | None = None,
) -> SenderRule | None:
    scoped = scope_chat_id(kind, chat_id)
    query = select(SenderRule).where(SenderRule.kind == kind, SenderRule.key == key)
    if scoped is None:
        query = query.where(SenderRule.chat_id.is_(None))
    else:
        query = query.where(SenderRule.chat_id == scoped)
    return await session.scalar(query.limit(1))


async def find_rule(
    session: AsyncSession,
    actor_kind: TransportActorKind,
    tg_user_id: int | None,
    chat_id: int | None,
) -> SenderRule | None:
    """Правило для этого отправителя, если человек про него решал."""
    kind = kind_for(actor_kind, tg_user_id)
    if kind is None:
        return None
    if kind is SenderRuleKind.ANONYMOUS_ADMIN and chat_id is None:
        # Без чата решение анонимного админа неопределимо: не переносить решение
        # одного чата на все остальные.
        return None
    return await load_rule(session, kind, int(tg_user_id), chat_id)


# ═══════════════════════════════════════════════════════════════
# Применение и отмена — задним числом
# ═══════════════════════════════════════════════════════════════


async def apply_rule(
    session: AsyncSession,
    rule: SenderRule | None,
    previous: SenderRule | RuleSnapshot | None,
) -> dict[str, int]:
    """ЕДИНСТВЕННОЕ место, где правило меняет данные:
      • стороны сообщений в области правила — через `resolve_business_side`, как в приёме,
        поэтому отмена возвращает ответ кода без отдельной ветки;
      • `side_rule_version` — текущий;
      • вердикт стирается и ставится `needs_reclassification` ТОЛЬКО при перевороте
        клиент↔компания (`reprocess.invalidate_side_change`);
      • атрибуция пересчитывается, у ушедших со стороны компании снимается.
    Обращения пересобирает штатный тик воркера (все чаты целиком).
    """
    from app.services.attribution import attribute_message
    from app.services.ingestion import SIDE_RULE_VERSION, resolve_business_side
    from app.services.reprocess import invalidate_side_change

    anchor = rule or previous
    if anchor is None:
        return {"messages": 0, "changed": 0, "reclassify": 0, "chats": 0}

    kind: SenderRuleKind = anchor.kind
    key = int(anchor.key)
    chat_id = scope_chat_id(kind, anchor.chat_id)

    # Область правила — один набор условий и для пересчёта, и для удаления строк автора.
    scope = [
        Message.tg_user_id == key,
        Message.transport_actor_kind.in_(RULED_ACTOR_KINDS),
        # Без текста и вложения — служебное событие (та же страховка, что в `recompute_sides`).
        or_(Message.text.isnot(None), Message.has_media.is_(True)),
    ]
    if chat_id is not None:
        scope.append(Message.chat_id == chat_id)

    touched: list[int] = []
    reclassify: list[int] = []
    left_company: list[int] = []
    chats: set[int] = set()
    changed = 0
    human_sides = (BusinessSide.CLIENT, BusinessSide.COMPANY)

    for message in (
        await session.scalars(select(Message).where(*scope).order_by(Message.id))
    ).all():
        previous_side = message.business_side
        side = await resolve_business_side(
            session,
            message.transport_actor_kind,
            message.tg_user_id,
            message.text,
            chat_id=message.chat_id,
        )
        touched.append(message.id)
        chats.add(message.chat_id)
        message.side_rule_version = SIDE_RULE_VERSION
        if side is not previous_side:
            if side in human_sides and previous_side in human_sides:
                reclassify.append(message.id)
                message.needs_reclassification = True
            if previous_side is BusinessSide.COMPANY and side is not BusinessSide.COMPANY:
                left_company.append(message.id)
            message.business_side = side
            changed += 1

    await invalidate_side_change(session, reclassify, left_company)

    if touched:
        # Строки автора этих сообщений создало само правило (или код по Telegram ID).
        # Ручную разметку подписей это не задевает: сообщения интегратора вне области правила.
        # Явное удаление нужно: строка правила помечена MANUAL, upsert её не тронет.
        await session.execute(
            delete(Attribution).where(
                Attribution.message_id.in_(select(Message.id).where(*scope))
            )
        )
        await session.flush()
        for message in (
            await session.scalars(
                select(Message)
                .where(*scope)
                .where(Message.business_side == BusinessSide.COMPANY)
                .order_by(Message.id)
            )
        ).all():
            await attribute_message(session, message)

    log.info(
        "sender_rule.applied",
        kind=kind.value,
        key=key,
        chat_id=chat_id,
        side=rule.side.value if rule is not None else None,
        messages=len(touched),
        changed=changed,
        reclassify=len(reclassify),
        chats=len(chats),
    )
    return {
        "messages": len(touched),
        "changed": changed,
        "reclassify": len(reclassify),
        "chats": len(chats),
    }


async def set_rule(
    session: AsyncSession,
    actor: BotUser,
    *,
    kind: SenderRuleKind,
    key: int,
    chat_id: int | None,
    side: SenderRuleSide,
    staff_id: int | None = None,
    display: str | None = None,
    note: str | None = None,
) -> tuple[SenderRule, dict[str, int]]:
    require_perm(actor, Perm.ATTRIBUTION_ASSIGN)

    scoped = scope_chat_id(kind, chat_id)
    if kind is SenderRuleKind.ANONYMOUS_ADMIN and scoped is None:
        # Учётка «от имени группы» одна на весь Telegram: правило без чата применилось бы
        # ко всем группам.
        raise ValueError("Решение про анонимного админа действует только в чате")
    if side is not SenderRuleSide.COMPANY:
        # Автор-сотрудник бывает только у компании.
        staff_id = None

    rule = await load_rule(session, kind, key, scoped)
    before = snapshot(rule)
    if rule is None:
        rule = SenderRule(kind=kind, key=key, chat_id=scoped)
        session.add(rule)
    rule.side = side
    rule.staff_id = staff_id
    rule.decided_by = actor.id
    rule.decided_at = datetime.now(timezone.utc)
    if display:
        rule.display = display[:128]
    if note is not None:
        rule.note = note
    await session.flush()

    stats = await apply_rule(session, rule, before)
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="sender_rule.set",
            object_type="sender_rule",
            object_id=str(rule.id),
            payload={
                "kind": kind.value,
                "key": key,
                "chat_id": scoped,
                "side": side.value,
                "staff_id": staff_id,
                "display": rule.display,
                "previous": (
                    {"side": before.side.value, "staff_id": before.staff_id}
                    if before is not None
                    else None
                ),
                **stats,
            },
        )
    )
    return rule, stats


async def clear_rule(
    session: AsyncSession, actor: BotUser, rule: SenderRule
) -> dict[str, int]:
    """Сбросить решение: отправитель снова определяется правилами кода."""
    require_perm(actor, Perm.ATTRIBUTION_ASSIGN)

    before = snapshot(rule)
    rule_id = rule.id
    await session.delete(rule)
    await session.flush()

    stats = await apply_rule(session, None, before)
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="sender_rule.cleared",
            object_type="sender_rule",
            object_id=str(rule_id),
            payload={
                "kind": before.kind.value,
                "key": before.key,
                "chat_id": before.chat_id,
                "side": before.side.value,
                "staff_id": before.staff_id,
                **stats,
            },
        )
    )
    return stats


# ═══════════════════════════════════════════════════════════════
# Кто это: имя, статистика, объяснение текущей стороны
# ═══════════════════════════════════════════════════════════════


async def sender_display(
    session: AsyncSession, tg_user_id: int, tg_chat_id: int | None = None
) -> str:
    """Имя отправителя: «Иван Петров», «@ivan» или «id 1234567». В `message` имени нет —
    берём из последнего сырого апдейта этого пользователя.
    """
    sender = TelegramUpdate.payload["message"]["from"]
    query = (
        select(sender)
        .where(TelegramUpdate.update_type == "message")
        .where(sender["id"].astext == str(tg_user_id))
    )
    if tg_chat_id is not None:
        query = query.where(
            TelegramUpdate.payload["message"]["chat"]["id"].astext == str(tg_chat_id)
        )
    row = await session.scalar(query.order_by(TelegramUpdate.update_id.desc()).limit(1))

    if isinstance(row, dict):
        name = " ".join(
            part for part in (row.get("first_name"), row.get("last_name")) if part
        ).strip()
        if name:
            return name[:128]
        username = row.get("username")
        if username:
            return f"@{username}"[:128]
    return f"id {tg_user_id}"


async def display_for(
    session: AsyncSession, kind: SenderRuleKind, key: int, chat_id: int | None
) -> str:
    """Подпись отправителя с поправкой на анонимного админа: у него имени нет,
    поэтому карточка подписана чатом.
    """
    if kind is SenderRuleKind.ANONYMOUS_ADMIN:
        title = None
        if chat_id is not None:
            title = await session.scalar(select(Chat.title).where(Chat.id == chat_id))
        return (f"от имени группы «{title}»" if title else "от имени группы")[:128]

    tg_chat_id = None
    if chat_id is not None:
        tg_chat_id = await session.scalar(select(Chat.tg_chat_id).where(Chat.id == chat_id))
    name = await sender_display(session, key, tg_chat_id)
    if name.startswith("id ") and tg_chat_id is not None:
        # В этом чате апдейта нет — ищем в остальных: человек тот же.
        name = await sender_display(session, key)
    return name


async def sender_chats(
    session: AsyncSession, key: int, chat_id: int | None = None
) -> list[tuple[str, int]]:
    query = (
        select(Chat.title, func.count(Message.id).label("messages"))
        .join(Chat, Chat.id == Message.chat_id)
        .where(Message.tg_user_id == key)
        .where(Message.transport_actor_kind.in_(RULED_ACTOR_KINDS))
        .group_by(Chat.id, Chat.title)
        .order_by(func.count(Message.id).desc())
    )
    if chat_id is not None:
        query = query.where(Message.chat_id == chat_id)
    return [(row.title or "?", int(row.messages)) for row in (await session.execute(query)).all()]


async def last_message(
    session: AsyncSession, key: int, chat_id: int | None = None
) -> Message | None:
    query = (
        select(Message)
        .where(Message.tg_user_id == key)
        .where(Message.transport_actor_kind.in_(RULED_ACTOR_KINDS))
    )
    if chat_id is not None:
        query = query.where(Message.chat_id == chat_id)
    return await session.scalar(
        query.order_by(Message.sent_at.desc(), Message.id.desc()).limit(1)
    )


# Почему сторона такая, если правила нет, — показывается на экране.
_CODE_REASONS = {
    SenderRuleKind.ANONYMOUS_ADMIN: (
        "автоматически: писали «от имени группы», а так может только её админ — "
        "сотрудник компании"
    ),
    SenderRuleKind.BOT: "автоматически: чужой бот — кто за ним стоит, неизвестно",
    SenderRuleKind.TG_USER: (
        "автоматически: участник чата без учётной записи в боте — считается клиентом"
    ),
}


async def explain_side(
    session: AsyncSession, kind: SenderRuleKind, key: int, chat_id: int | None
) -> tuple[SenderRule | None, BusinessSide, str]:
    from app.services.ingestion import resolve_business_side

    actor_kind = (
        TransportActorKind.HUMAN_USER
        if kind is SenderRuleKind.TG_USER
        else TransportActorKind.OTHER_BOT
    )
    side = await resolve_business_side(session, actor_kind, key, None, chat_id=chat_id)
    rule = await load_rule(session, kind, key, chat_id)
    if rule is None:
        reason = _CODE_REASONS[kind]
        if side is BusinessSide.COMPANY and kind is SenderRuleKind.TG_USER:
            reason = "автоматически: есть учётная запись в боте — сотрудник компании"
        return None, side, reason

    decided = rule.decided_at.strftime("%d.%m") if rule.decided_at else "?"
    who = None
    if rule.decided_by is not None:
        who = await session.scalar(
            select(BotUser.display_name).where(BotUser.id == rule.decided_by)
        )
    return rule, side, f"по правилу от {decided}" + (f", {who}" if who else "")


async def unruled_bot_senders(session: AsyncSession, limit: int = 8) -> list[dict]:
    """Чужие боты без решения — записи очереди разметки. Анонимного админа
    `unknown_bot_senders` исключает: он разбирается из переписки конкретного чата.
    """
    from app.services.reprocess import unknown_bot_senders

    decided = set(
        (
            await session.scalars(
                select(SenderRule.key).where(SenderRule.kind == SenderRuleKind.BOT)
            )
        ).all()
    )
    rows = [
        row
        for row in await unknown_bot_senders(session, limit=limit + len(decided))
        if row["tg_user_id"] not in decided
    ]
    return rows[:limit]


# ═══════════════════════════════════════════════════════════════
# Подсказка по ответу Битрикса на «/auth»
# ═══════════════════════════════════════════════════════════════

# «@user Вы авторизованы на <портал> как Имя Фамилия». Ведущее упоминание
# снимается той же функцией, что у приёма.
_AUTH_NAME = re.compile(
    r"вы\s+авторизованы\b.*?\bкак\s+(?P<name>[^\n.,;!?]{3,80})",
    re.IGNORECASE | re.DOTALL,
)


def parse_auth_name(text: str | None) -> str | None:
    if not text:
        return None
    match = _AUTH_NAME.search(strip_leading_mention(text))
    if match is None:
        return None
    name = " ".join(match.group("name").split()).strip()
    return name or None


async def auth_hint(
    session: AsyncSession, chat_id: int
) -> tuple[Staff, datetime] | None:
    """«Похоже, это Имя Фамилия» — по авторизации портала в этом чате.
    Только подсказка: правило она не создаёт.
    """
    from app.services.attribution import _find_staff

    rows = (
        await session.execute(
            select(Message.text, Message.sent_at)
            .where(Message.chat_id == chat_id)
            .where(Message.business_side == BusinessSide.INTEGRATOR_SYSTEM)
            .where(Message.text.isnot(None))
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(50)
        )
    ).all()
    for text, sent_at in rows:
        name = parse_auth_name(text)
        if not name:
            continue
        staff = await _find_staff(session, name)
        if staff is not None:
            return staff, sent_at
    return None


# ═══════════════════════════════════════════════════════════════
# «Участники» показанного окна выписки
# ═══════════════════════════════════════════════════════════════

_SIDE_MARKS = {
    BusinessSide.CLIENT: "🔵",
    BusinessSide.COMPANY: "🟢",
    BusinessSide.INTEGRATOR_SYSTEM: "⚙️",
    BusinessSide.UNKNOWN: "⚪",
}


async def window_participants(
    session: AsyncSession, chat_id: int, from_id: int, to_id: int
) -> list[dict]:
    """Кто говорил в показанном куске выписки — по записи на отправителя.

    Границы — id крайних показанных сообщений (кнопка несёт их с собой, состояния нет).
    Сообщения интегратора с разобранной подписью не попадают; с неразобранной —
    ведут в карточку подписи.
    """
    rows = (
        await session.execute(
            select(
                Message.tg_user_id,
                Message.transport_actor_kind,
                Message.business_side,
                Message.id,
                Attribution.staff_id,
                Attribution.raw_name,
                Attribution.method,
            )
            .outerjoin(Attribution, Attribution.message_id == Message.id)
            .where(Message.chat_id == chat_id)
            .where(Message.id >= from_id)
            .where(Message.id <= to_id)
            # Та же видимость, что у `transcript.load_around`.
            .where(
                or_(
                    Message.transport_actor_kind != TransportActorKind.TELEGRAM_SYSTEM,
                    Message.text.isnot(None),
                    Message.has_media.is_(True),
                )
            )
            .order_by(Message.id)
        )
    ).all()

    tg_chat_id = await session.scalar(select(Chat.tg_chat_id).where(Chat.id == chat_id))
    numbers = await client_numbers(session, chat_id)
    senders: dict[tuple, dict] = {}
    signatures: dict[str | None, dict] = {}

    for tg_user_id, actor_kind, side, message_id, staff_id, raw_name, method in rows:
        if actor_kind is TransportActorKind.INTEGRATOR_BOT:
            if staff_id is not None or method is AttributionMethod.MANUAL:
                continue  # автор известен или решение уже принято
            entry = signatures.setdefault(
                raw_name,
                {"kind": "signature", "anchor": message_id, "raw_name": raw_name, "messages": 0},
            )
            entry["messages"] += 1
            continue

        kind = kind_for(actor_kind, tg_user_id)
        if kind is None:
            continue
        scoped = scope_chat_id(kind, chat_id)
        entry = senders.setdefault(
            (kind, tg_user_id, scoped),
            {
                "kind": "sender",
                "sender_kind": kind,
                "key": int(tg_user_id),
                "chat_id": scoped,
                "side": side,
                "messages": 0,
            },
        )
        entry["messages"] += 1
        entry["side"] = side

    result: list[dict] = []
    for entry in senders.values():
        rule = await load_rule(session, entry["sender_kind"], entry["key"], entry["chat_id"])
        entry["rule"] = rule is not None
        entry["label"] = await _participant_label(
            session, entry, rule, tg_chat_id, numbers.get(entry["key"])
        )
        result.append(entry)
    result.sort(key=lambda item: -item["messages"])

    for entry in sorted(signatures.values(), key=lambda item: -item["messages"]):
        label = entry["raw_name"] or "без подписи"
        entry["label"] = f"⚪ {label} (Битрикс, автора не узнали)"
        result.append(entry)
    return result


async def _participant_label(
    session: AsyncSession,
    entry: dict,
    rule: SenderRule | None,
    tg_chat_id: int | None,
    client_number: int | None = None,
) -> str:
    """«🔵 Иван (клиент 2)», «🟢 компания (анонимно)», «⚪ бот 1234567».
    `client_number` — тот же номер, что в алерте и выписке (`transcript.client_numbers`)."""
    kind: SenderRuleKind = entry["sender_kind"]
    side: BusinessSide = entry["side"]
    mark = _SIDE_MARKS.get(side, "⚪")

    if kind is SenderRuleKind.ANONYMOUS_ADMIN:
        name = "компания (анонимно)" if side is BusinessSide.COMPANY else "от имени группы"
    elif kind is SenderRuleKind.BOT:
        name = f"бот {entry['key']}"
    else:
        name = (rule.display if rule and rule.display else None) or await sender_display(
            session, entry["key"], tg_chat_id
        )

    if rule is not None and rule.staff_id is not None:
        person = await session.scalar(
            select(Staff.full_name).where(Staff.id == rule.staff_id)
        )
        if person:
            return f"{mark} {person} (сотрудник, по правилу)"

    if rule is not None:
        return f"{mark} {name} ({SIDE_LABELS[rule.side]}, по правилу)"
    if kind is SenderRuleKind.ANONYMOUS_ADMIN:
        return f"{mark} {name}"
    if side is BusinessSide.CLIENT:
        number = f" {client_number}" if client_number else ""
        return f"{mark} {name} (клиент{number})"
    if side is BusinessSide.COMPANY:
        return f"{mark} {name} (компания)"
    return f"{mark} {name}"
