"""Приём данных из Telegram: только сохранить то, что прислал Telegram.

Слой НЕ интерпретирует и ничего не отбрасывает (docs/ARCHITECTURE.md, раздел 1):
ошибка парсера, вшитого в приём, означала бы безвозвратно потерянные данные.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, is_notify_group
from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    ChatTitleHistory,
    Message,
    Staff,
    TelegramUpdate,
    TransportActorKind,
)

log = structlog.get_logger(__name__)

# Версия правила стороны (`resolve_business_side`, docs/ARCHITECTURE.md §5); пишется
# в `message.side_rule_version`. Смена версии пересчитывает стороны при старте
# воркера (reprocess.sync_integrator_change).
SIDE_RULE_VERSION = 5

# Служебная учётка Telegram для сообщений «от имени группы» — публичный бот
# @GroupAnonymousBot (один на весь Telegram).
# Сторона — КОМПАНИЯ: так писать может только админ группы, а админы клиентских
# групп — сотрудники. Автор при этом неизвестен.
ANONYMOUS_ADMIN_BOT_ID = 1087968824

# Разрешённый состав пользовательского сообщения: всё, что не текст и не одно
# из этих вложений, — служебное. Новый тип события Telegram не откроет ложное обращение.
_USER_CONTENT_FIELDS = (
    "text",
    "caption",
    "photo",
    "document",
    "video",
    "voice",
    "audio",
    "animation",
    "sticker",
    "video_note",
    "contact",
    "location",
    "venue",
    "poll",
    "dice",
    "game",
    "story",
    "paid_media",
)

# Служебные события Telegram, оформленные как message («добавлен участник»):
# отправителем значится человек, совершивший действие.
_SERVICE_KEYS = (
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "pinned_message",
    "message_auto_delete_timer_changed",
    "migrate_to_chat_id",
    "migrate_from_chat_id",
    "forum_topic_created",
    "forum_topic_edited",
    "forum_topic_closed",
    "forum_topic_reopened",
)

_MEDIA_FIELDS = (
    "photo",
    "document",
    "video",
    "voice",
    "audio",
    "animation",
    "sticker",
    "video_note",
)


async def store_raw_update(session: AsyncSession, update_id: int, update_type: str, payload: dict) -> bool:
    """Записать апдейт в неизменяемый журнал. False — такой update_id уже есть
    (нормально при переподключении polling).
    """
    stmt = (
        pg_insert(TelegramUpdate)
        .values(update_id=update_id, update_type=update_type, payload=payload)
        .on_conflict_do_nothing(index_elements=["update_id"])
        .returning(TelegramUpdate.update_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def mark_update_processed(
    session: AsyncSession, update_id: int, error: str | None = None
) -> None:
    update = await session.get(TelegramUpdate, update_id)
    if update is None:
        return
    update.processed_at = datetime.now(timezone.utc)
    update.processing_error = error


# ═══════════════════════════════════════════════════════════════
# Определение стороны
# ═══════════════════════════════════════════════════════════════


def resolve_transport_actor(raw_message: dict) -> tuple[TransportActorKind, int | None]:
    """Кто прислал — только по Telegram ID и типу апдейта, без разбора текста."""
    settings = get_settings()
    # aiogram отдаёт поле как "from" или "from_user" в зависимости от сериализации.
    sender = raw_message.get("from") or raw_message.get("from_user") or {}
    tg_user_id = sender.get("id")

    if tg_user_id is None:
        return TransportActorKind.TELEGRAM_SYSTEM, None

    if settings.integrator_bot_id and tg_user_id == settings.integrator_bot_id:
        return TransportActorKind.INTEGRATOR_BOT, tg_user_id

    if sender.get("is_bot"):
        return TransportActorKind.OTHER_BOT, tg_user_id

    return TransportActorKind.HUMAN_USER, tg_user_id


async def resolve_business_side(
    session: AsyncSession,
    actor_kind: TransportActorKind,
    tg_user_id: int | None,
    text: str | None,
    *,
    chat_id: int | None = None,
) -> BusinessSide:
    """Чья сторона; производное значение.

    РЕШЕНИЕ ЧЕЛОВЕКА ПЕРЕКРЫВАЕТ КОД: правило отправителя (`sender_rule`) проверяется
    первым, тем же путём и в пересчёте (`reprocess.recompute_sides`). Сообщение известного
    интегратора остаётся company, даже если ФИО не распознано. `chat_id` — внутренний id
    чата, нужен правилу анонимного админа.
    """
    if actor_kind is TransportActorKind.TELEGRAM_SYSTEM:
        # Служебное событие Telegram не бывает сообщением клиента или компании,
        # каким бы правилом ни был помечен его автор.
        return BusinessSide.UNKNOWN

    # `session is None` — вызов без базы (проверки правил кода): правило отправителя
    # спросить негде.
    if session is not None and actor_kind in (
        TransportActorKind.HUMAN_USER,
        TransportActorKind.OTHER_BOT,
    ):
        from app.services.sender_rules import SIDE_BY_RULE, find_rule

        rule = await find_rule(session, actor_kind, tg_user_id, chat_id)
        if rule is not None:
            return SIDE_BY_RULE[rule.side]

    # «От имени группы» — админ группы, то есть сотрудник. Прочие чужие боты — ничьи.
    if actor_kind is TransportActorKind.OTHER_BOT and tg_user_id == ANONYMOUS_ADMIN_BOT_ID:
        return BusinessSide.COMPANY

    if actor_kind is TransportActorKind.INTEGRATOR_BOT:
        # Служебные сообщения интегратора не человеческая активность.
        if _looks_like_service_message(text):
            return BusinessSide.INTEGRATOR_SYSTEM
        # Подпись «(К)» — человек со стороны клиента через тот же портал.
        from app.services.attribution import is_client_signature, parse_integrator_prefix

        if is_client_signature(parse_integrator_prefix(text)):
            return BusinessSide.CLIENT
        return BusinessSide.COMPANY

    if actor_kind is TransportActorKind.HUMAN_USER and tg_user_id is not None:
        # Сотрудник пишет в Telegram напрямую: узнаётся по Staff.tg_user_id или по учётке бота.
        known_staff = await session.scalar(select(Staff.id).where(Staff.tg_user_id == tg_user_id))
        if known_staff is not None:
            return BusinessSide.COMPANY
        from app.db.models import BotUser, BotUserState

        # Учётка в боте выдаётся только своим; для стороны привязка к справочнику не нужна.
        # PENDING не берём: запись заводится и незнакомцу после /start. Отключённую учётку —
        # только с привязкой (уволенный сотрудник остаётся стороной компании).
        account = await session.scalar(
            select(BotUser.id)
            .where(BotUser.tg_user_id == tg_user_id)
            .where(
                or_(
                    BotUser.state == BotUserState.ACTIVE,
                    BotUser.staff_id.isnot(None),
                )
            )
        )
        if account is not None:
            return BusinessSide.COMPANY
        return BusinessSide.CLIENT

    return BusinessSide.UNKNOWN


# Ведущее упоминание, которым Битрикс адресует ответ набравшему команду;
# снимается до проверки маркеров, чтобы ответ не зависел от адресата.
_LEADING_MENTION = re.compile(r"^\s*@[A-Za-z0-9_]{1,32}\s*")


def strip_leading_mention(text: str) -> str:
    """Текст без ведущего «@упоминания» и пробельных символов по краям. Публичная: тем же ключом
    сравнивает очередь разметки (attribution.not_staff_fingerprint).
    """
    return _LEADING_MENTION.sub("", text or "").strip()


# Начала служебных ответов интегратора; сверяются с НАЧАЛОМ текста без учёта
# регистра (продолжение у них разное). Подписи у таких ответов нет.
_SERVICE_MARKERS = (
    "бот подключ",
    "интеграция",
    "новый участник",
    "участник добавлен",
    "чат создан",
    # Диалог авторизации портала.
    "вы не авторизованы",
    "вы авторизованы",
    "если хотите переавторизоваться",
    "вы успешно привязаны",
    "вы уже привязаны",
    "пересылка сообщений из этого чата",
    "id этого телеграм чата",
)


def _looks_like_service_message(text: str | None) -> bool:
    """Эвристика служебного сообщения интегратора. Ошибка не фатальна:
    business_side версионируется и пересчитывается.
    """
    if not text:
        return False
    lowered = strip_leading_mention(text).lower()
    return any(lowered.startswith(marker) for marker in _SERVICE_MARKERS)


# ═══════════════════════════════════════════════════════════════
# Чаты
# ═══════════════════════════════════════════════════════════════


async def get_or_create_chat(
    session: AsyncSession, raw_chat: dict, *, at: datetime | None = None
) -> Chat:
    """Найти чат или зарегистрировать в состоянии discovered; сообщения пишутся сразу.
    `at` — время события, которым чат обнаружен: автовключение открывает наблюдение
    с него, иначе первое сообщение выпало бы из периода.
    """
    tg_chat_id = raw_chat["id"]
    title = raw_chat.get("title")

    from app.services.settings_store import get_value

    auto_track = bool(await get_value(session, "chats", "auto_track_new"))
    # Группа уведомлений не автовключается никогда (страховка к проверке в ingest_message).
    if is_notify_group(tg_chat_id):
        auto_track = False
    initial_state = ChatState.TRACKED if auto_track else ChatState.DISCOVERED
    initial_tracked_since = (at or datetime.now(timezone.utc)) if auto_track else None

    # Upsert вместо SELECT→INSERT: апдейты обрабатываются параллельно.
    inserted = await session.execute(
        pg_insert(Chat)
        .values(
            tg_chat_id=tg_chat_id,
            title=title,
            state=initial_state,
            tracked_since=initial_tracked_since,
            is_forum=bool(raw_chat.get("is_forum")),
            settings={},
        )
        .on_conflict_do_nothing(index_elements=["tg_chat_id"])
        .returning(Chat.id)
    )
    new_id = inserted.scalar_one_or_none()
    chat = await session.scalar(select(Chat).where(Chat.tg_chat_id == tg_chat_id))

    if new_id is not None:
        session.add(ChatTitleHistory(chat_id=chat.id, title=title))
        if auto_track:
            # Интервал наблюдения открывается вместе с чатом.
            from app.services.tracking import open_period

            await open_period(session, chat, reason="auto_track", at=initial_tracked_since)
        log.info(
            "chat.discovered",
            tg_chat_id=tg_chat_id,
            title=title,
            is_forum=chat.is_forum,
            auto_tracked=auto_track,
        )
        return chat

    if title and title != chat.title:
        session.add(ChatTitleHistory(chat_id=chat.id, title=title))
        chat.title = title
        log.info("chat.renamed", tg_chat_id=tg_chat_id, title=title)

    if raw_chat.get("is_forum") and not chat.is_forum:
        chat.is_forum = True

    return chat


# ═══════════════════════════════════════════════════════════════
# Сообщения
# ═══════════════════════════════════════════════════════════════


def is_service_message(raw_message: dict) -> bool:
    """Служебное ли это событие. Проверка от разрешённого: пользовательское — только
    с текстом или знакомым вложением.
    """
    if any(key in raw_message for key in _SERVICE_KEYS):
        return True
    return not any(raw_message.get(field) for field in _USER_CONTENT_FIELDS)


def _detect_media(raw_message: dict) -> tuple[bool, str | None]:
    for field in _MEDIA_FIELDS:
        if raw_message.get(field):
            return True, field
    return False, None


def _extract_text(raw_message: dict) -> str | None:
    return raw_message.get("text") or raw_message.get("caption")


async def ingest_message(
    session: AsyncSession, raw_message: dict, *, is_edit: bool = False
) -> Message | None:
    raw_chat = raw_message.get("chat")
    if not raw_chat:
        return None

    # Переезд группы уведомлений ловится здесь: Telegram присылает migrate_to_chat_id
    # в старую группу и migrate_from_chat_id в новую.
    migrated_to = raw_message.get("migrate_to_chat_id")
    migrated_from = raw_message.get("migrate_from_chat_id")
    if migrated_to and is_notify_group(raw_chat.get("id")):
        from app.services.notify_group import record_migration

        await record_migration(session, int(migrated_to))
        return None
    if migrated_from and is_notify_group(migrated_from):
        from app.services.notify_group import record_migration

        await record_migration(session, int(raw_chat.get("id")))
        return None

    # Группа уведомлений не инжестится (сырой журнал пишется): бот не должен
    # алертить о собственных уведомлениях.
    if is_notify_group(raw_chat.get("id")):
        return None

    chat = await get_or_create_chat(session, raw_chat, at=_ts(raw_message.get("date")))
    tg_message_id = raw_message["message_id"]

    existing = await session.scalar(
        select(Message).where(
            Message.chat_id == chat.id, Message.tg_message_id == tg_message_id
        )
    )

    text = _extract_text(raw_message)
    has_media, media_kind = _detect_media(raw_message)

    if existing is not None:
        if is_edit:
            existing.text = text
            existing.entities = raw_message.get("entities") or raw_message.get("caption_entities")
            existing.char_count = len(text or "")
            existing.edited_at = _ts(raw_message.get("edit_date"))
            # Правка могла изменить смысл — нужна переклассификация.
            existing.needs_reclassification = True
            # Правка могла сменить и СТОРОНУ (подпись «(К)» появилась или исчезла).
            if existing.transport_actor_kind is not TransportActorKind.TELEGRAM_SYSTEM:
                side = await resolve_business_side(
                    session,
                    existing.transport_actor_kind,
                    existing.tg_user_id,
                    text,
                    chat_id=existing.chat_id,
                )
                if side is not existing.business_side:
                    from app.services.reprocess import invalidate_side_change

                    previous_side = existing.business_side
                    existing.business_side = side
                    existing.side_rule_version = SIDE_RULE_VERSION
                    # Ушло со стороны компании — строка атрибуции не нужна.
                    left_company = (
                        [existing.id]
                        if previous_side is BusinessSide.COMPANY
                        and side is not BusinessSide.COMPANY
                        else []
                    )
                    await invalidate_side_change(session, [existing.id], left_company)
                    log.info(
                        "message.edited.side_changed",
                        chat_id=chat.id,
                        tg_message_id=tg_message_id,
                        previous=previous_side.value,
                        current=side.value,
                    )
            log.info("message.edited", chat_id=chat.id, tg_message_id=tg_message_id)
        return existing

    actor_kind, tg_user_id = resolve_transport_actor(raw_message)
    if is_service_message(raw_message):
        actor_kind = TransportActorKind.TELEGRAM_SYSTEM
        side = BusinessSide.UNKNOWN
        media_kind = media_kind or "service"
    else:
        side = await resolve_business_side(
            session, actor_kind, tg_user_id, text, chat_id=chat.id
        )

    # on_conflict_do_nothing: гонка и повторная доставка, а не «проверили — вставили».
    result = await session.execute(
        pg_insert(Message)
        .values(
            chat_id=chat.id,
            thread_id=raw_message.get("message_thread_id"),
            tg_message_id=tg_message_id,
            tg_user_id=tg_user_id,
            transport_actor_kind=actor_kind,
            business_side=side,
            side_rule_version=SIDE_RULE_VERSION,
            text=text,
            entities=raw_message.get("entities") or raw_message.get("caption_entities"),
            char_count=len(text or ""),
            media_kind=media_kind,
            has_media=has_media,
            needs_reclassification=False,
            reply_to_tg_message_id=(raw_message.get("reply_to_message") or {}).get("message_id"),
            sent_at=_ts(raw_message.get("date")) or datetime.now(timezone.utc),
            edited_at=_ts(raw_message.get("edit_date")),
        )
        .on_conflict_do_nothing(constraint="uq_message_chat_tgid")
        .returning(Message.id)
    )
    message_id = result.scalar_one_or_none()
    if message_id is None:
        # Проиграли гонку — запись уже есть.
        return await session.scalar(
            select(Message).where(
                Message.chat_id == chat.id, Message.tg_message_id == tg_message_id
            )
        )

    message = await session.get(Message, message_id)

    # Атрибуция на лету — best-effort: её ошибка не роняет приём. SAVEPOINT обязателен:
    # ошибка SQL без него прерывает всю транзакцию, и сообщение не сохранилось бы.
    if message is not None and side is BusinessSide.COMPANY:
        try:
            from app.services.attribution import attribute_message

            async with session.begin_nested():
                await attribute_message(session, message)
        except Exception:
            log.exception("ingest.attribution_failed", message_id=message.id)

    return message


def _ts(value: Any) -> datetime | None:
    """Время из апдейта: unix-секунды, ISO-строка или datetime (зависит от сериализации
    aiogram). Всегда timezone-aware UTC.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(value, tz=timezone.utc)
