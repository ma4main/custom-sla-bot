"""Запрет на запись в групповые чаты, политика fail-closed.

В группах бот молчит всегда и пишет только в служебную группу уведомлений
(docs/SCREENS.md, раздел 0). Для группы разрешён лишь явный список читающих
методов; всё остальное, включая будущие методы Bot API, блокируется до запроса.
"""

from __future__ import annotations

from typing import Any, Callable

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.methods import Response, TelegramMethod
from aiogram.methods.base import TelegramType

log = structlog.get_logger(__name__)


class GroupWriteAttempt(RuntimeError):
    """Попытка изменить что-либо в групповом чате. Всегда ошибка проектирования."""


# Единственные методы для групповых chat_id: только чтение, участникам не видно.
_GROUP_READ_ALLOWLIST = frozenset(
    {
        "GetChat",
        "GetChatMember",
        "GetChatMemberCount",
        "GetChatAdministrators",
        "GetForumTopicIconStickers",
        "GetChatMenuButton",
    }
)

# Группа уведомлений — единственная, куда бот пишет: текст (алерты, сводки),
# файл (выгрузки) и правка своего сообщения, чтобы отметить закрытый алерт
# (Telegram даёт боту править только свои сообщения). Остальное запрещено.
_NOTIFY_GROUP_SEND_ALLOWLIST = frozenset(
    {"SendMessage", "SendDocument", "EditMessageText"}
)


def _is_group(chat_id: Any) -> bool:
    if isinstance(chat_id, int):
        return chat_id < 0
    if isinstance(chat_id, str) and (chat_id.startswith("-") or chat_id.startswith("@")):
        return True
    return False


def blocked_reason(
    method_name: str,
    chat_id: Any,
    inline_message_id: Any,
    notify_group_id: int | None = None,
) -> str | None:
    """Причина блокировки или None, если вызов разрешён.

    Чистая функция: тест перебирает все методы Bot API.
    """
    # inline_message_id адресует сообщение без chat_id; inline mode выключен —
    # запрещаем весь класс.
    if inline_message_id is not None:
        return "изменение по inline_message_id запрещено"

    if chat_id is None or not _is_group(chat_id):
        return None

    if method_name in _GROUP_READ_ALLOWLIST:
        return None

    # Строго int: строковый id («-555…», «@имя») группой уведомлений не считается.
    if (
        notify_group_id is not None
        and isinstance(chat_id, int)
        and chat_id == notify_group_id
    ):
        if method_name in _NOTIFY_GROUP_SEND_ALLOWLIST:
            return None
        return (
            "группе уведомлений разрешены только SendMessage, SendDocument "
            "и EditMessageText"
        )

    return "группам разрешено только чтение из явного списка"


class OutboundGroupGuard(BaseRequestMiddleware):
    def __init__(
        self,
        notify_group_id: int | None = None,
        resolver: Callable[[], int | None] | None = None,
    ) -> None:
        # resolver отдаёт действующий номер группы на момент вызова: переезд
        # в супергруппу меняет его без рестарта. notify_group_id — для тестов.
        self._notify_group_id = notify_group_id
        self._resolver = resolver

    def _current_group_id(self) -> int | None:
        if self._resolver is not None:
            return self._resolver()
        return self._notify_group_id

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        name = type(method).__name__
        reason = blocked_reason(
            name,
            getattr(method, "chat_id", None),
            getattr(method, "inline_message_id", None),
            self._current_group_id(),
        )
        if reason is not None:
            log.error(
                "outbound.blocked",
                method=name,
                chat_id=getattr(method, "chat_id", None),
                reason=reason,
            )
            raise GroupWriteAttempt(f"Заблокирован вызов {name}: {reason}")

        return await make_request(bot, method)


def create_bot(token: str) -> Bot:
    """Единственная точка создания Bot: созданный напрямую экземпляр заслона не имеет."""
    from app.config import effective_notify_group_id

    bot = Bot(token=token, default=DefaultBotProperties())
    bot.session.middleware(OutboundGroupGuard(resolver=effective_notify_group_id))
    return bot
