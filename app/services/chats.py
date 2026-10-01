"""Управление чатами: включение, пауза, архив, удаление."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, BotUser, Chat, ChatState, Message
from app.services.tracking import open_period, sync_period
from app.services.access import Perm, require_perm

log = structlog.get_logger(__name__)


class ChatError(RuntimeError):
    """Действие над чатом запрещено правилами."""


STATE_LABELS = {
    ChatState.DISCOVERED: "🆕 обнаружен",
    ChatState.TRACKED: "✅ в анализе",
    ChatState.PAUSED: "⏸ на паузе",
    ChatState.ARCHIVED: "📦 в архиве",
}


async def list_chats(
    session: AsyncSession, state: ChatState | None = None, *, offset: int = 0, limit: int = 8
) -> tuple[list[Chat], int]:
    stmt = select(Chat)
    count_stmt = select(func.count(Chat.id))
    if state is not None:
        stmt = stmt.where(Chat.state == state)
        count_stmt = count_stmt.where(Chat.state == state)

    total = await session.scalar(count_stmt) or 0
    rows = (
        await session.scalars(stmt.order_by(Chat.title.nulls_last(), Chat.id).offset(offset).limit(limit))
    ).all()
    return list(rows), total


async def search_chats(
    session: AsyncSession, query: str, *, limit: int = 10
) -> tuple[list[Chat], int]:
    """Чаты по части названия во всех состояниях, без учёта регистра; «%» и «_»
    означают сами себя. Без листания: строка запроса живёт в форме.
    """
    needle = (query or "").strip()
    if not needle:
        return [], 0
    pattern = (
        "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )
    condition = Chat.title.ilike(pattern, escape="\\")
    total = await session.scalar(select(func.count(Chat.id)).where(condition)) or 0
    rows = (
        await session.scalars(
            select(Chat).where(condition).order_by(Chat.title.nulls_last(), Chat.id).limit(limit)
        )
    ).all()
    return list(rows), total


async def count_by_state(session: AsyncSession) -> dict[ChatState, int]:
    rows = await session.execute(select(Chat.state, func.count(Chat.id)).group_by(Chat.state))
    return {state: count for state, count in rows.all()}


async def message_count(session: AsyncSession, chat_id: int) -> int:
    return await session.scalar(
        select(func.count(Message.id)).where(Message.chat_id == chat_id)
    ) or 0


async def track_all_discovered(session: AsyncSession, actor: BotUser) -> int:
    require_perm(actor, Perm.CHAT_MANAGE)
    now = datetime.now(timezone.utc)

    chats = (
        await session.scalars(select(Chat).where(Chat.state == ChatState.DISCOVERED))
    ).all()
    for chat in chats:
        chat.state = ChatState.TRACKED
        if chat.tracked_since is None:
            chat.tracked_since = now
        await open_period(session, chat, reason="track_all", at=now)

    if chats:
        session.add(
            AuditLog(
                actor_user_id=actor.id,
                action="chat.track_all",
                object_type="chat",
                payload={"count": len(chats), "titles": [ch.title for ch in chats][:20]},
            )
        )
        log.info("chat.track_all", count=len(chats), actor=actor.tg_user_id)
    return len(chats)


async def delete_chat(session: AsyncSession, actor: BotUser, chat: Chat) -> dict[str, Any]:
    """Убрать чат из системы вместе с собранным по нему.

    ⚠️ Только для архивных: удаление чата в анализе молча унесло бы живые данные.
    Каскадом удаляется всё производное; сырой журнал апдейтов остаётся.
    """
    require_perm(actor, Perm.CHAT_MANAGE)

    if chat.state is not ChatState.ARCHIVED:
        raise ChatError(
            "Удалить можно только чат из архива. Если чат больше не нужен, "
            "сначала удалите бота из группы — чат уйдёт в архив сам."
        )

    summary = {
        "title": chat.title,
        "tg_chat_id": chat.tg_chat_id,
        "messages": await message_count(session, chat.id),
    }

    # Журнал пишется ДО удаления: после него не останется ни chat_id, ни названия.
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="chat.deleted",
            object_type="chat",
            object_id=str(chat.id),
            payload=summary,
        )
    )
    await session.flush()
    await session.delete(chat)

    log.info(
        "chat.deleted",
        chat_id=chat.id,
        title=chat.title,
        messages=summary["messages"],
        actor=actor.tg_user_id,
    )
    return summary


async def set_state(
    session: AsyncSession, actor: BotUser, chat: Chat, state: ChatState
) -> None:
    """Сменить состояние чата. Пауза отключает алерты по чату, поэтому пишется в журнал."""
    require_perm(actor, Perm.CHAT_MANAGE)
    previous = chat.state

    if state is ChatState.TRACKED and chat.tracked_since is None:
        # Момент первого включения фиксируется навсегда: «ноль сообщений» ≠ «ещё не отслеживался».
        chat.tracked_since = datetime.now(timezone.utc)

    if state is ChatState.ARCHIVED:
        chat.archived_at = datetime.now(timezone.utc)
    elif previous is ChatState.ARCHIVED:
        chat.archived_at = None

    chat.state = state
    # Интервалы наблюдения ведутся отдельно: пауза сегодня не меняет прошлые отчёты.
    await sync_period(session, chat, state, reason=f"state:{state.value}")

    session.add(
        AuditLog(
            actor_user_id=actor.id,
            action="chat.state_changed",
            object_type="chat",
            object_id=str(chat.id),
            payload={"from": previous.value, "to": state.value, "title": chat.title},
        )
    )
    log.info(
        "chat.state_changed",
        chat_id=chat.id,
        title=chat.title,
        from_state=previous.value,
        to_state=state.value,
        actor=actor.tg_user_id,
    )
