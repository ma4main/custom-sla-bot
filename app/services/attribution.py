"""Атрибуция авторства: кто из сотрудников написал сообщение.

Формат интегратора: «Имя Фамилия [домен] пишет:», пустая строка, текст (или
«… делится файлом»). Порядок Имя-Фамилия, внутри домена бывает zero-width space.
Атрибуция производна; staff_id = NULL — автор не определён, ждёт ручной разметки.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import structlog
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Attribution,
    AttributionMethod,
    AuditLog,
    BotUser,
    BusinessSide,
    Message,
    Setting,
    Staff,
    StaffAlias,
    TransportActorKind,
)
from app.services.access import Perm, require_perm
from app.services.ingestion import strip_leading_mention
from app.services.staff import normalize_name

log = structlog.get_logger(__name__)

PARSER_VERSION = 2

# Невидимые символы интегратора (ZWSP в домене) вычищаются до разбора.
_INVISIBLES = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]")

# «Имя Фамилия [домен] пишет:» либо «… делится файлом» в начале сообщения.
# Имя — до скобки, без предположений о числе слов (отчества, дефисы).
_PREFIX = re.compile(
    r"^\s*(?P<name>[^\[\]\n]{1,80}?)\s*\[[^\]]{0,120}\]\s*"
    r"(?:пишет\s*:|делится\s+файлом)\s*",
    re.IGNORECASE,
)


# Метка «(К)» перед именем — человек со стороны клиента, подключённый к порталу:
# приходит тем же интегратором, но это сторона клиента. Латинская K — на случай раскладки.
_CLIENT_MARK = re.compile(r"^\(\s*[КK]\s*\)", re.IGNORECASE)


def is_client_signature(name: str | None) -> bool:
    return bool(name) and _CLIENT_MARK.match(name.strip()) is not None


def parse_integrator_prefix(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = _INVISIBLES.sub("", text)
    match = _PREFIX.match(cleaned)
    if match is None:
        return None
    name = match.group("name").strip()
    return name or None


def strip_integrator_prefix(text: str | None) -> str:
    """Текст без служебного префикса — для показа человеку (автор подписан отдельно)."""
    if not text:
        return ""
    cleaned = _INVISIBLES.sub("", text)
    match = _PREFIX.match(cleaned)
    if match is None:
        return cleaned.strip()
    return cleaned[match.end() :].strip()


def _order_key(normalized: str) -> str:
    """Ключ сравнения без учёта порядка слов: «Ирина Соколова» = «Соколова Ирина»."""
    return " ".join(sorted(normalized.split()))


async def _find_staff(session: AsyncSession, raw_name: str) -> Staff | None:
    """Совпадение по нормализованному имени или алиасу, порядок слов не важен.
    Только активные сотрудники: уволившийся не поглощает сообщения однофамильца.
    """
    normalized = normalize_name(raw_name)
    exact = await session.scalar(
        select(Staff)
        .outerjoin(StaffAlias, StaffAlias.staff_id == Staff.id)
        .where(Staff.active.is_(True))
        .where(
            or_(
                Staff.normalized_name == normalized,
                StaffAlias.alias == normalized,
            )
        )
        .limit(1)
    )
    if exact is not None:
        return exact

    # Перестановка слов; справочник маленький, сравнение в Python.
    # Ключ совпал с ДВУМЯ людьми — unresolved: приписать наугад хуже, чем спросить.
    key = _order_key(normalized)
    people = (
        await session.scalars(select(Staff).where(Staff.active.is_(True)))
    ).all()
    matched = []
    for person in people:
        keys = {_order_key(person.normalized_name)}
        keys.update(_order_key(alias.alias) for alias in person.aliases)
        if key in keys:
            matched.append(person)
    if len(matched) == 1:
        return matched[0]
    return None


_NAME_SHAPE = re.compile(r"^[А-ЯЁA-Z][а-яёa-z-]+ [А-ЯЁA-Z][а-яёa-z-]+( [А-ЯЁA-Z][а-яёa-z-]+)?$")


async def _auto_create_staff(session: AsyncSession, raw_name: str):
    """Завести сотрудника из префикса интегратора (имени из портала можно доверять).
    Строгая форма «Имя Фамилия [Отчество]»; при коллизии — unresolved.
    """
    if not _NAME_SHAPE.match(raw_name.strip()):
        return None

    staff = Staff(
        full_name=raw_name.strip(),
        normalized_name=normalize_name(raw_name),
    )
    try:
        # SAVEPOINT, а не rollback сессии: откат уничтожил бы и само сообщение.
        async with session.begin_nested():
            session.add(staff)
            await session.flush()
    except Exception:
        # Однофамилец или гонка — решает человек через разметку.
        return None

    session.add(
        AuditLog(
            actor_user_id=None,
            action="staff.auto_created",
            object_type="staff",
            object_id=str(staff.id),
            payload={"full_name": staff.full_name, "source": "integrator_prefix"},
        )
    )
    log.info("staff.auto_created", full_name=staff.full_name)
    return staff


async def _decided_not_staff(session: AsyncSession, raw_name: str) -> bool:
    found = await session.scalar(
        select(Attribution.message_id)
        .where(Attribution.raw_name == raw_name)
        .where(not_staff_condition())
        .limit(1)
    )
    return found is not None


# ═══════════════════════════════════════════════════════════════
# «Не сотрудник» для сообщений БЕЗ подписи
# ═══════════════════════════════════════════════════════════════

# Решение «не сотрудник» для сообщений без подписи (служебные ответы интегратора)
# запоминается по НАЧАЛУ текста: оно постоянное, а хвост у каждого свой.
# Хранится в setting вне DEFAULTS — это память решений, а не настройка.
NOT_STAFF_STATE_KEY = "attribution_marks"
NOT_STAFF_TEXTS_FIELD = "not_staff_texts"

# Длина шаблона: различает служебные ответы, но не разводит одно решение
# на два из-за имени в конце строки.
FINGERPRINT_LIMIT = 80


def not_staff_fingerprint(text: str | None) -> str | None:
    if not text:
        return None
    collapsed = " ".join(strip_leading_mention(text).split()).lower()
    return collapsed[:FINGERPRINT_LIMIT] or None


async def _not_staff_texts(session: AsyncSession) -> list[str]:
    stored = await session.get(Setting, NOT_STAFF_STATE_KEY)
    if stored is None or not isinstance(stored.value, dict):
        return []
    values = stored.value.get(NOT_STAFF_TEXTS_FIELD)
    return [str(item) for item in values] if isinstance(values, list) else []


async def _store_not_staff_texts(session: AsyncSession, texts: list[str]) -> None:
    stored = await session.get(Setting, NOT_STAFF_STATE_KEY)
    if stored is None:
        session.add(
            Setting(key=NOT_STAFF_STATE_KEY, value={NOT_STAFF_TEXTS_FIELD: texts})
        )
    else:
        stored.value = {**(stored.value or {}), NOT_STAFF_TEXTS_FIELD: texts}


async def _decided_not_staff_text(session: AsyncSession, text: str | None) -> bool:
    fingerprint = not_staff_fingerprint(text)
    if fingerprint is None:
        return False
    return fingerprint in await _not_staff_texts(session)


async def attribute_message(session: AsyncSession, message: Message) -> None:
    """Разметить одно сообщение. Ошибки не глушит: приём зовёт её в точке сохранения
    и сам ловит исключение, чтобы сообщение сохранилось без автора."""
    staff_id: int | None = None
    method: AttributionMethod | None = None
    raw_name: str | None = None
    confidence: float | None = None
    assigned_by: int | None = None
    assigned_at: datetime | None = None

    if (
        message.transport_actor_kind is TransportActorKind.INTEGRATOR_BOT
        and message.business_side is BusinessSide.COMPANY
    ):
        raw_name = parse_integrator_prefix(message.text)
        if raw_name:
            staff = await _find_staff(session, raw_name)
            if staff is None:
                staff = await _auto_create_staff(session, raw_name)
            if staff is not None:
                staff_id = staff.id
                method = AttributionMethod.EXACT
                confidence = 1.0
            elif await _decided_not_staff(session, raw_name):
                # Человек уже решил, что эта ПОДПИСЬ не сотрудника: решение относится к подписи,
                # иначе каждая новая рассылка возвращалась бы в очередь.
                method = AttributionMethod.MANUAL
                confidence = 1.0
        elif await _decided_not_staff_text(session, message.text):
            # Подписи нет — решение сверяется по шаблону текста.
            method = AttributionMethod.MANUAL
            confidence = 1.0
    elif (
        message.transport_actor_kind
        in (TransportActorKind.HUMAN_USER, TransportActorKind.OTHER_BOT)
        and message.business_side is BusinessSide.COMPANY
        and message.tg_user_id is not None
    ):
        # Решение человека про отправителя — первым: он мог назвать автором того,
        # кого по ID в справочнике не найти.
        from app.services.sender_rules import find_rule

        rule = await find_rule(
            session, message.transport_actor_kind, message.tg_user_id, message.chat_id
        )
        if rule is not None and rule.staff_id is not None:
            staff_id = rule.staff_id
            method = AttributionMethod.MANUAL
            confidence = 1.0
            assigned_by = rule.decided_by
            assigned_at = rule.decided_at
        elif message.transport_actor_kind is TransportActorKind.OTHER_BOT:
            # Бот на стороне компании без решения человека: автора нет, а в очередь разметки
            # звать некуда — там выбирают по подписи.
            return
        else:
            # Сотрудник пишет в Telegram напрямую — автор известен по ID
            # (тот же путь, что у стороны в ingestion.resolve_business_side).
            staff = await session.scalar(
                select(Staff).where(Staff.tg_user_id == message.tg_user_id).limit(1)
            )
            if staff is None:
                # Привязка учётки бота к сотруднику тоже даёт автора: Staff.tg_user_id заполняется редко.
                staff = await session.scalar(
                    select(Staff)
                    .join(BotUser, BotUser.staff_id == Staff.id)
                    .where(BotUser.tg_user_id == message.tg_user_id)
                    .limit(1)
                )
            if staff is not None:
                staff_id = staff.id
                method = AttributionMethod.TG_ID
                confidence = 1.0
    else:
        # Сообщения клиентов и служебные не атрибутируются.
        return

    await session.execute(
        _attribution_upsert(
            message.id, staff_id, method, confidence, raw_name, assigned_by, assigned_at
        )
    )


def _attribution_upsert(
    message_id: int,
    staff_id: int | None,
    method: AttributionMethod | None,
    confidence: float | None,
    raw_name: str | None,
    assigned_by: int | None = None,
    assigned_at: datetime | None = None,
):
    """Вставить или обновить строку автора, не затирая решений человека."""
    values = {
        "message_id": message_id,
        "staff_id": staff_id,
        "method": method,
        "confidence": confidence,
        "parser_version": PARSER_VERSION,
        "raw_name": raw_name,
        "assigned_by": assigned_by,
        "assigned_at": assigned_at,
    }
    return (
        pg_insert(Attribution)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["message_id"],
            set_={key: value for key, value in values.items() if key != "message_id"},
            # Ручную разметку пересчёт не затирает.
            where=or_(
                Attribution.method.is_(None),
                Attribution.method != AttributionMethod.MANUAL,
            ),
        )
    )


async def attribute_all(session: AsyncSession, batch_size: int = 500) -> dict[str, int]:
    """Разметить все сообщения компании: новые и размеченные старым парсером.
    Идём по последнему обработанному id, а не по смещению: обработанные строки
    выпадают из выборки, и offset перепрыгивал бы через партии.
    """
    total = 0
    after_id = 0

    while True:
        rows = (
            await session.scalars(
                select(Message)
                .outerjoin(Attribution, Attribution.message_id == Message.id)
                .where(Message.business_side == BusinessSide.COMPANY)
                .where(Message.id > after_id)
                .where(
                    or_(
                        Attribution.message_id.is_(None),
                        and_(
                            or_(
                                Attribution.parser_version < PARSER_VERSION,
                                # Нераспознанные — всегда: справочник мог пополниться.
                                Attribution.staff_id.is_(None),
                            ),
                            # Ручную разметку (и «не сотрудник») не выбираем: пересчёт её не трогает,
                            # и строки навсегда остались бы «необработанными».
                            or_(
                                Attribution.method.is_(None),
                                Attribution.method != AttributionMethod.MANUAL,
                            ),
                        ),
                    )
                )
                .order_by(Message.id)
                .limit(batch_size)
            )
        ).all()
        if not rows:
            break
        for message in rows:
            await attribute_message(session, message)
            total += 1
        after_id = rows[-1].id

    resolved = await session.scalar(
        select(func.count(Attribution.message_id)).where(Attribution.staff_id.isnot(None))
    ) or 0
    unresolved = await count_unresolved(session)

    log.info("attribution.done", processed=total, resolved=resolved, unresolved=unresolved)
    return {"processed": total, "resolved": resolved, "unresolved": unresolved}


# ═══════════════════════════════════════════════════════════════
# Очередь разметки: подписи, которых нет в справочнике
# ═══════════════════════════════════════════════════════════════

# «Эта подпись — не сотрудник» хранится без новой колонки: staff_id = NULL,
# method = MANUAL и автор решения. Пересчёт такие строки не трогает, а очередь
# отличает «ещё не разобрались» от «разобрались: это не человек».


def unresolved_condition():
    return and_(
        Attribution.staff_id.is_(None),
        or_(
            Attribution.method.is_(None),
            Attribution.method != AttributionMethod.MANUAL,
        ),
    )


def not_staff_condition():
    return and_(
        Attribution.staff_id.is_(None),
        Attribution.method == AttributionMethod.MANUAL,
    )


async def count_unresolved(session: AsyncSession) -> int:
    return await session.scalar(
        select(func.count(Attribution.message_id)).where(unresolved_condition())
    ) or 0


async def count_not_staff(session: AsyncSession) -> int:
    return await session.scalar(
        select(func.count(Attribution.message_id)).where(not_staff_condition())
    ) or 0


async def _groups(session: AsyncSession, condition, limit: int) -> list[tuple[str | None, int, int]]:
    """Подписи: (подпись, сколько сообщений, якорное сообщение). Подпись не влезает
    в callback data, поэтому кнопка несёт id якоря, а решение применяется ко всей группе.
    """
    rows = (
        await session.execute(
            select(
                Attribution.raw_name,
                func.count().label("count"),
                func.min(Attribution.message_id).label("anchor"),
            )
            .where(condition)
            .group_by(Attribution.raw_name)
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()
    return [(row.raw_name, int(row.count), int(row.anchor)) for row in rows]


async def unresolved_groups(
    session: AsyncSession, limit: int = 8
) -> list[tuple[str | None, int, int]]:
    return await _groups(session, unresolved_condition(), limit)


async def not_staff_groups(
    session: AsyncSession, limit: int = 8
) -> list[tuple[str | None, int, int]]:
    return await _groups(session, not_staff_condition(), limit)


async def _same_text_ids(session: AsyncSession, anchor_id: int, condition) -> list[int]:
    """Сообщения без подписи, совпадающие с якорем по шаблону текста. Отбор в Python:
    нормализация должна быть ОДНА с той, по которой сверяется приём (not_staff_fingerprint).
    """
    anchor_text = await session.scalar(select(Message.text).where(Message.id == anchor_id))
    fingerprint = not_staff_fingerprint(anchor_text)
    if fingerprint is None:
        return [anchor_id]
    rows = (
        await session.execute(
            select(Attribution.message_id, Message.text)
            .join(Message, Message.id == Attribution.message_id)
            .where(Attribution.raw_name.is_(None))
            .where(condition)
        )
    ).all()
    found = [
        message_id
        for message_id, text in rows
        if not_staff_fingerprint(text) == fingerprint
    ]
    return found or [anchor_id]


async def group_size(session: AsyncSession, anchor_id: int) -> int:
    anchor = await session.get(Attribution, anchor_id)
    if anchor is None:
        return 0
    if anchor.raw_name:
        return await session.scalar(
            select(func.count(Attribution.message_id))
            .where(unresolved_condition())
            .where(Attribution.raw_name == anchor.raw_name)
        ) or 0
    return len(await _same_text_ids(session, anchor_id, unresolved_condition()))


async def not_staff_group_size(session: AsyncSession, anchor_id: int) -> int:
    """Сколько сообщений группы помечено «не сотрудник». Отдельно от `group_size`:
    другое условие, а `raw_name = NULL` в SQL всегда ложно.
    """
    anchor = await session.get(Attribution, anchor_id)
    if anchor is None:
        return 0
    if anchor.raw_name:
        return await session.scalar(
            select(func.count(Attribution.message_id))
            .where(not_staff_condition())
            .where(Attribution.raw_name == anchor.raw_name)
        ) or 0
    return len(await _same_text_ids(session, anchor_id, not_staff_condition()))


async def _apply_to_group(
    session: AsyncSession, anchor_id: int, *, condition, values: dict
) -> tuple[int, str | None]:
    anchor = await session.get(Attribution, anchor_id)
    if anchor is None:
        return 0, None

    stmt = update(Attribution).where(condition)
    if anchor.raw_name:
        stmt = stmt.where(Attribution.raw_name == anchor.raw_name)
    else:
        # Подписи нет — группируем по шаблону текста.
        stmt = stmt.where(
            Attribution.message_id.in_(await _same_text_ids(session, anchor_id, condition))
        )

    result = await session.execute(stmt.values(**values))
    return result.rowcount or 0, anchor.raw_name


async def bind_raw_name(
    session: AsyncSession, actor: BotUser, anchor_id: int, staff_id: int
) -> int:
    require_perm(actor, Perm.ATTRIBUTION_ASSIGN)

    count, raw_name = await _apply_to_group(
        session,
        anchor_id,
        condition=unresolved_condition(),
        values={
            "staff_id": staff_id,
            "method": AttributionMethod.MANUAL,
            "confidence": 1.0,
            "assigned_by": actor.id,
            "assigned_at": datetime.now(timezone.utc),
        },
    )
    if count:
        session.add(
            AuditLog(
                actor_user_id=actor.id,
                action="attribution.manual",
                object_type="staff",
                object_id=str(staff_id),
                payload={"raw_name": raw_name, "count": count},
            )
        )
    return count


async def mark_not_staff(session: AsyncSession, actor: BotUser, anchor_id: int) -> int:
    """Пометить подпись как не принадлежащую сотруднику. Для сообщений без подписи
    решение запоминается шаблоном текста.
    """
    require_perm(actor, Perm.ATTRIBUTION_ASSIGN)

    anchor_text = await session.scalar(select(Message.text).where(Message.id == anchor_id))
    count, raw_name = await _apply_to_group(
        session,
        anchor_id,
        condition=unresolved_condition(),
        values={
            "staff_id": None,
            "method": AttributionMethod.MANUAL,
            "confidence": 1.0,
            "assigned_by": actor.id,
            "assigned_at": datetime.now(timezone.utc),
        },
    )
    fingerprint = None
    if count and raw_name is None:
        fingerprint = not_staff_fingerprint(anchor_text)
        if fingerprint is not None:
            texts = await _not_staff_texts(session)
            if fingerprint not in texts:
                await _store_not_staff_texts(session, [*texts, fingerprint])
    if count:
        session.add(
            AuditLog(
                actor_user_id=actor.id,
                action="attribution.not_staff",
                object_type="attribution",
                object_id=str(anchor_id),
                payload={"raw_name": raw_name, "count": count, "text": fingerprint},
            )
        )
        log.info(
            "attribution.not_staff", raw_name=raw_name, count=count, text=fingerprint
        )
    return count


async def restore_to_queue(session: AsyncSession, actor: BotUser, anchor_id: int) -> int:
    """Вернуть помеченную подпись в очередь. Снимает и запомненный шаблон текста,
    иначе приём тут же пометил бы строки обратно.
    """
    require_perm(actor, Perm.ATTRIBUTION_ASSIGN)

    anchor_text = await session.scalar(select(Message.text).where(Message.id == anchor_id))
    count, raw_name = await _apply_to_group(
        session,
        anchor_id,
        condition=not_staff_condition(),
        values={
            "method": None,
            "confidence": None,
            "assigned_by": None,
            "assigned_at": None,
        },
    )
    if count and raw_name is None:
        fingerprint = not_staff_fingerprint(anchor_text)
        texts = await _not_staff_texts(session)
        if fingerprint is not None and fingerprint in texts:
            await _store_not_staff_texts(
                session, [item for item in texts if item != fingerprint]
            )
    if count:
        session.add(
            AuditLog(
                actor_user_id=actor.id,
                action="attribution.restored",
                object_type="attribution",
                object_id=str(anchor_id),
                payload={"raw_name": raw_name, "count": count},
            )
        )
    return count
