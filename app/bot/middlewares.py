"""Middleware приёма."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.db.base import session_scope
from app.db.models import BotUserState
from app.services.access import get_user, has_perm
from app.health import beat
from app.services.ingestion import mark_update_processed, store_raw_update
from app.text import NEUTRAL_REPLY

log = structlog.get_logger(__name__)


def dump_update(event: Update) -> dict:
    """JSON апдейта для журнала — ровно то, что прислал Telegram.

    exclude_unset обязателен: незаданные поля aiogram заполняет сентинелом
    Default(...), который pydantic не сериализует.
    """
    return event.model_dump(mode="json", exclude_none=True, exclude_unset=True)


class RawUpdateJournalMiddleware(BaseMiddleware):
    """Пишет каждый апдейт в журнал до обработчиков.

    update_id — первичный ключ: повторная доставка не создаёт дублей,
    а сырой апдейт можно обработать заново.
    """

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        update_id = event.update_id
        update_type = event.event_type

        # Апдейт дошёл — приём работает. Отметка ставится до обработчиков:
        # упавший обработчик не значит, что polling мёртв.
        beat("bot")

        try:
            payload = dump_update(event)
        except Exception as exc:  # noqa: BLE001 — журнал не вправе глушить приём
            # Сбой сериализации теряет сырьё одного апдейта, но не сам апдейт:
            # заглушка сохраняет идемпотентность по update_id.
            log.exception("update.payload_failed", update_id=update_id, update_type=update_type)
            payload = {
                "_unserializable": True,
                "update_type": update_type,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }

        async with session_scope() as session:
            is_new = await store_raw_update(
                session,
                update_id=update_id,
                update_type=update_type,
                payload=payload,
            )

        if not is_new:
            # Повторная доставка при переподключении — штатная ситуация.
            log.debug("update.duplicate", update_id=update_id, update_type=update_type)
            return None

        error: str | None = None
        try:
            return await handler(event, data)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.exception("update.handler_failed", update_id=update_id, update_type=update_type)
            # Кнопка не должна остаться со спиннером. Отвечаем здесь, а не
            # в dispatcher.errors: тот проглотил бы исключение до журнала и re-raise.
            query = getattr(event, "callback_query", None)
            if query is not None:
                try:
                    await query.answer(
                        "Действие сейчас не выполнилось. Попробуйте ещё раз "
                        "или откройте свежее меню: /menu. Мы уже записали, "
                        "что случилось.",
                        show_alert=True,
                    )
                except Exception:  # noqa: BLE001 — отвечать больше нечем
                    pass
            raise
        finally:
            async with session_scope() as session:
                await mark_update_processed(session, update_id, error)


class AuthMiddleware(BaseMiddleware):
    """Пропускает дальше только активных пользователей с нужным правом.

    Право проверяется на каждом событии: кнопки в старых сообщениях после
    понижения роли не работают. Скрытая кнопка — удобство, а не авторизация.
    """

    def __init__(self, required_perm: str | None = None) -> None:
        self.required_perm = required_perm

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None:
            return None

        async with session_scope() as session:
            bot_user = await get_user(session, tg_user.id)
            if bot_user is not None:
                # Отвязываем от сессии: дальше объект используется только на чтение.
                session.expunge(bot_user)

        if bot_user is None or bot_user.state is not BotUserState.ACTIVE:
            if isinstance(event, CallbackQuery):
                await event.answer(NEUTRAL_REPLY, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(NEUTRAL_REPLY)
            return None

        if self.required_perm is not None and not has_perm(bot_user, self.required_perm):
            log.info(
                "auth.denied",
                tg_user_id=bot_user.tg_user_id,
                role=bot_user.role.value,
                perm=self.required_perm,
            )
            if isinstance(event, CallbackQuery):
                await event.answer("Недостаточно прав", show_alert=True)
            return None

        data["bot_user"] = bot_user
        return await handler(event, data)
