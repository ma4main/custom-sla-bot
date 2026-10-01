"""Пересчёт производных данных (сторона, автор, состояние чата) по сохранённому сырью.

См. принцип «сырьё отдельно от интерпретации» в docs/ARCHITECTURE.md §1.
"""

from __future__ import annotations

import structlog
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Attribution,
    AttributionMethod,
    BusinessSide,
    Classification,
    Message,
    TransportActorKind,
)
from app.services.ingestion import (
    ANONYMOUS_ADMIN_BOT_ID,
    SIDE_RULE_VERSION,
    resolve_business_side,
)

log = structlog.get_logger(__name__)


async def unknown_bot_senders(session: AsyncSession, limit: int = 10) -> list[dict]:
    """Боты, писавшие в чаты, кроме нашего, — для опознания интегратора.

    Анонимный админ группы не попадает: его сторона уже определена (компания).
    """
    rows = await session.execute(
        select(
            Message.tg_user_id,
            func.count(Message.id).label("messages"),
            func.max(Message.sent_at).label("last_seen"),
            func.min(Message.text).label("sample"),
        )
        .where(Message.transport_actor_kind == TransportActorKind.OTHER_BOT)
        .where(Message.tg_user_id != ANONYMOUS_ADMIN_BOT_ID)
        .group_by(Message.tg_user_id)
        .order_by(func.count(Message.id).desc())
        .limit(limit)
    )
    return [
        {
            "tg_user_id": row.tg_user_id,
            "messages": row.messages,
            "last_seen": row.last_seen,
            "sample": (row.sample or "")[:120],
        }
        for row in rows.all()
    ]


# Типы апдейтов, которые можно проиграть заново. message/edited_message идут
# через идемпотентный ingest, my_chat_member/chat_member — через обработчик
# жизненного цикла чата. callback_query не переигрывается намеренно: это
# нажатие человека, повтор выполнил бы действие второй раз.
REPLAYABLE_MESSAGE_TYPES = ("message", "edited_message")
REPLAYABLE_MEMBERSHIP_TYPES = ("my_chat_member", "chat_member")
REPLAYABLE_TYPES = REPLAYABLE_MESSAGE_TYPES + REPLAYABLE_MEMBERSHIP_TYPES


async def replay_failed_updates(session: AsyncSession, limit: int = 200) -> dict[str, int]:
    """Повторно прогнать упавшие апдейты через их обработчики (payload сохранён до обработчика)."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import and_

    from app.db.models import TelegramUpdate
    from app.services.ingestion import ingest_message

    # Также берутся записанные, но не обработанные без ошибки (процесс упал
    # между журналом и обработчиком); порог возраста не даёт схватить апдейт,
    # который прямо сейчас обрабатывает живой бот.
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=10)
    rows = (
        await session.scalars(
            select(TelegramUpdate)
            .where(
                or_(
                    TelegramUpdate.processing_error.isnot(None),
                    and_(
                        TelegramUpdate.processed_at.is_(None),
                        TelegramUpdate.processing_error.is_(None),
                        TelegramUpdate.received_at < stale_before,
                    ),
                )
            )
            .where(TelegramUpdate.update_type.in_(REPLAYABLE_TYPES))
            .order_by(TelegramUpdate.update_id)
            .limit(limit)
        )
    ).all()

    # Упавшие нажатия кнопок считаются отдельно, чтобы число «найдено
    # с ошибкой» в интерфейсе сходилось.
    skipped = await session.scalar(
        select(func.count(TelegramUpdate.update_id))
        .where(TelegramUpdate.processing_error.isnot(None))
        .where(TelegramUpdate.update_type.notin_(REPLAYABLE_TYPES))
    ) or 0

    membership_rows = [
        row for row in rows if row.update_type in REPLAYABLE_MEMBERSHIP_TYPES
    ]
    latest_membership = await _latest_membership_update_ids(session, membership_rows)

    succeeded = 0
    failed = 0
    stale = 0
    for row in rows:
        raw = row.payload.get(row.update_type)
        if not raw:
            row.processing_error = "payload без тела события"
            failed += 1
            continue

        # Каждый повтор — в SAVEPOINT: ошибка SQL одного апдейта иначе прерывает общую
        # транзакцию, и не сохранились бы ни соседние повторы, ни запись об ошибке.
        try:
            if row.update_type in REPLAYABLE_MESSAGE_TYPES:
                async with session.begin_nested():
                    await ingest_message(
                        session, raw, is_edit=row.update_type == "edited_message"
                    )
            else:
                # Устаревшее членство не переигрываем: бота могли удалить и вернуть,
                # а проигранное «удалили» заархивировало бы живой чат.
                chat_id = (raw.get("chat") or {}).get("id")
                if latest_membership.get(chat_id) not in (None, row.update_id):
                    row.processing_error = (
                        "неактуально: по этому чату есть более позднее "
                        "событие о составе участников"
                    )
                    stale += 1
                    continue
                async with session.begin_nested():
                    await _replay_membership(session, raw)

            row.processing_error = None
            row.processed_at = datetime.now(timezone.utc)
            succeeded += 1
        except Exception as exc:  # noqa: BLE001 — ошибка пишется в журнал
            row.processing_error = f"{type(exc).__name__}: {exc}"
            failed += 1

    log.info(
        "reprocess.replay_done",
        found=len(rows),
        succeeded=succeeded,
        failed=failed,
        stale=stale,
        skipped=skipped,
    )
    return {
        "found": len(rows),
        "succeeded": succeeded,
        "failed": failed,
        "stale": stale,
        "skipped": skipped,
    }


async def _latest_membership_update_ids(session: AsyncSession, rows) -> dict[int, int]:
    """Самый свежий апдейт о составе участников по каждому затронутому чату.

    Нужен, чтобы не откатить состояние чата назад более старым событием.
    """
    from app.db.models import TelegramUpdate

    chat_ids = {
        (row.payload.get(row.update_type) or {}).get("chat", {}).get("id") for row in rows
    }
    chat_ids.discard(None)
    if not chat_ids:
        return {}

    latest: dict[int, int] = {}
    candidates = (
        await session.execute(
            select(TelegramUpdate.update_id, TelegramUpdate.update_type, TelegramUpdate.payload)
            .where(TelegramUpdate.update_type.in_(REPLAYABLE_MEMBERSHIP_TYPES))
            .order_by(TelegramUpdate.update_id)
        )
    ).all()
    for update_id, update_type, payload in candidates:
        chat_id = ((payload or {}).get(update_type) or {}).get("chat", {}).get("id")
        if chat_id in chat_ids:
            latest[chat_id] = update_id
    return latest


async def _replay_membership(session: AsyncSession, raw: dict) -> None:
    """Применить событие о составе участников к состоянию чата.

    Повторяет решения обработчика жизненного цикла на переданной сессии:
    прямой вызов обработчика открыл бы вложенную транзакцию.
    """
    from datetime import datetime, timezone

    from app.bot.handlers.chat_lifecycle import _ABSENT, _PRESENT, GROUP_CHAT_TYPES
    from app.db.models import ChatState
    from app.services.ingestion import get_or_create_chat
    from app.services.settings_store import get_value
    from app.services.tracking import close_period, open_period

    if (raw.get("chat") or {}).get("type") not in GROUP_CHAT_TYPES:
        # Как у обработчика: событие из лички рабочий чат не заводит.
        return
    chat = await get_or_create_chat(session, raw.get("chat") or {})
    status = ((raw.get("new_chat_member") or {}).get("status")) or ""

    if status in _ABSENT:
        chat.state = ChatState.ARCHIVED
        chat.archived_at = datetime.now(timezone.utc)
        await close_period(session, chat, reason="bot_removed")
        return

    if status in _PRESENT and chat.state is ChatState.ARCHIVED:
        auto_track = bool(await get_value(session, "chats", "auto_track_new"))
        chat.state = ChatState.TRACKED if auto_track else ChatState.DISCOVERED
        chat.archived_at = None
        if auto_track:
            if chat.tracked_since is None:
                chat.tracked_since = datetime.now(timezone.utc)
            await open_period(session, chat, reason="bot_returned")


# Ключ строки состояния: с каким INTEGRATOR_BOT_ID и версией правила стороны
# считались производные данные. Не настройка человека — память пересчёта.
INTEGRATOR_STATE_KEY = "reprocess_state"


async def sync_integrator_change(session: AsyncSession) -> bool:
    """Пересчитать стороны и авторов, если сменился ID интегратора
    или версия правила стороны. Возвращает, был ли пересчёт.

    Запись без версии считается версией 1.
    """
    from app.config import get_settings
    from app.db.models import Setting

    current = get_settings().integrator_bot_id
    stored_row = await session.get(Setting, INTEGRATOR_STATE_KEY)

    if stored_row is None:
        # Первый запуск: данные уже посчитаны текущими ID и правилом — запоминаем точку отсчёта.
        session.add(
            Setting(
                key=INTEGRATOR_STATE_KEY,
                value={
                    "integrator_bot_id": current,
                    "side_rule_version": SIDE_RULE_VERSION,
                },
            )
        )
        return False

    stored = (stored_row.value or {}).get("integrator_bot_id")
    stored_rule = int((stored_row.value or {}).get("side_rule_version") or 1)
    if stored == current and stored_rule == SIDE_RULE_VERSION:
        return False

    sides = await recompute_sides(session)
    # Сменившие сторону сообщения компании тоже нужно атрибутировать.
    from app.services.attribution import attribute_all

    attribution = await attribute_all(session)
    stored_row.value = {
        **(stored_row.value or {}),
        "integrator_bot_id": current,
        "side_rule_version": SIDE_RULE_VERSION,
    }
    log.info(
        "reprocess.integrator_changed",
        previous=stored,
        current=current,
        previous_rule=stored_rule,
        current_rule=SIDE_RULE_VERSION,
        sides_changed=sides["changed"],
        reclassify=sides["reclassify"],
        attributed=attribution["resolved"],
    )
    return True


async def invalidate_side_change(
    session: AsyncSession, reclassify: list[int], left_company: list[int]
) -> None:
    """Сбросить производное у сообщений, сменивших сторону, — одна процедура на все пути.

    Вердикт стирается (получен не тем промптом); у ушедших из компании
    (к клиенту или в служебные ответы интегратора) удаляется атрибуция, кроме
    ручной. Флаг `needs_reclassification` вызывающий ставит сам.
    """
    if reclassify:
        await session.execute(
            delete(Classification).where(Classification.message_id.in_(reclassify))
        )
    if left_company:
        await session.execute(
            delete(Attribution)
            .where(Attribution.message_id.in_(left_company))
            .where(
                or_(
                    Attribution.method.is_(None),
                    Attribution.method != AttributionMethod.MANUAL,
                )
            )
        )


async def recompute_sides(session: AsyncSession, batch_size: int = 500) -> dict[str, int]:
    """Пересчитать `business_side` и `transport_actor_kind` по всем сообщениям
    из сырого текста и отправителя."""
    changed = 0
    total = 0
    offset = 0
    # Сменившие сторону клиент↔компания: вердикт получен не тем промптом,
    # он стирается, а сообщение возвращается в очередь классификации.
    reclassify: list[int] = []
    # Ушедшие со стороны компании теряют строку атрибуции.
    left_company: list[int] = []
    human_sides = (BusinessSide.CLIENT, BusinessSide.COMPANY)

    from app.config import get_settings

    settings = get_settings()

    while True:
        rows = (
            await session.scalars(
                select(Message).order_by(Message.id).offset(offset).limit(batch_size)
            )
        ).all()
        if not rows:
            break

        for message in rows:
            total += 1
            previous_side = message.business_side

            # Служебное сообщение: ни текста, ни медиа (эвристика — сырых ключей
            # вроде new_chat_members в проекции нет).
            if message.text is None and not message.has_media:
                if (
                    message.transport_actor_kind is not TransportActorKind.TELEGRAM_SYSTEM
                    or message.business_side is not BusinessSide.UNKNOWN
                ):
                    if message.business_side is BusinessSide.COMPANY:
                        left_company.append(message.id)
                    message.transport_actor_kind = TransportActorKind.TELEGRAM_SYSTEM
                    message.business_side = BusinessSide.UNKNOWN
                    message.media_kind = message.media_kind or "service"
                    message.side_rule_version = SIDE_RULE_VERSION
                    changed += 1
                continue

            if message.tg_user_id is None:
                actor = TransportActorKind.TELEGRAM_SYSTEM
            elif settings.integrator_bot_id and message.tg_user_id == settings.integrator_bot_id:
                actor = TransportActorKind.INTEGRATOR_BOT
            elif message.transport_actor_kind in (
                TransportActorKind.OTHER_BOT,
                TransportActorKind.INTEGRATOR_BOT,
            ):
                actor = TransportActorKind.OTHER_BOT
            else:
                actor = TransportActorKind.HUMAN_USER

            # Тот же вызов, что на приёме: ручное правило отправителя приоритетнее
            # и пересчётом не затирается.
            side = await resolve_business_side(
                session, actor, message.tg_user_id, message.text, chat_id=message.chat_id
            )

            if actor != message.transport_actor_kind or side != previous_side:
                if side in human_sides and previous_side in human_sides and side != previous_side:
                    reclassify.append(message.id)
                    message.needs_reclassification = True
                if previous_side is BusinessSide.COMPANY and side is not BusinessSide.COMPANY:
                    left_company.append(message.id)
                message.transport_actor_kind = actor
                message.business_side = side
                message.side_rule_version = SIDE_RULE_VERSION
                changed += 1

        offset += batch_size

    await invalidate_side_change(session, reclassify, left_company)

    log.info(
        "reprocess.sides_done",
        total=total,
        changed=changed,
        reclassify=len(reclassify),
        left_company=len(left_company),
    )
    return {
        "total": total,
        "changed": changed,
        "reclassify": len(reclassify),
        "left_company": len(left_company),
    }
