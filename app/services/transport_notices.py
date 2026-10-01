"""Служебные ответы интегратора (`/auth`, `/info`, `/join_other_chat`, `/mute_outgoing_messages`),
не являющиеся репликами сотрудника.

Совпадение шаблона требуется целиком: незнакомый текст не подавляется.
"""
from __future__ import annotations

import re

from app.db.models import BusinessSide, Message, TransportActorKind
from app.services.attribution import parse_integrator_prefix


_NOTICES = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"(?:@\w+\s+)?(?:ID этого телеграм чата = -?\d+\s+)?"
    r"Вы не авторизованы[.!]?(?:\s+Воспользуйтесь командой /auth)?",
    r"(?:@\w+\s+)?Вы авторизованы на [\w.-]+ как [^\n]+",
    r"Если хотите переавторизоваться,? отправьте команду /auth с указанием портала[.!]?",
    r"Вы уже привязаны к этому чату[.!]?",
    r"Вы успешно привязаны к другому чату[.!]?",
    r"Пересылка сообщений ИЗ этого чата В другие привязанные чаты (?:ВКЛЮЧЕНА|ВЫКЛЮЧЕНА|ОСТАНОВЛЕНА)\."
    r"\s*Чтобы (?:прекратить|возобновить) пересылку, используйте команду [\"«]/mute_outgoing_messages(?: \d+)?[\"»]\.",
    r"Чат настроен[.!]?",
))


def is_integrator_notice(message: Message) -> bool:
    """Совпадение всего шаблона и бот-транспорт без подписи сотрудника: такая же фраза
    от человека может отвечать клиенту.
    """
    if (
        message.transport_actor_kind is not TransportActorKind.INTEGRATOR_BOT
        or message.business_side is not BusinessSide.COMPANY
        or not message.text
        or parse_integrator_prefix(message.text) is not None
    ):
        return False
    return any(pattern.fullmatch(message.text.strip()) for pattern in _NOTICES)
