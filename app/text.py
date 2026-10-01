"""Экранирование текста для Telegram HTML: названия чатов и имена приходят извне,
и `<` или `&` в них ломает разбор всего сообщения.
"""

from __future__ import annotations


def esc(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Единственный ответ незнакомцу в личке: нейтральный, не раскрывает систему ролей
# (docs/SCREENS.md, раздел 1). Заявка владельцу при этом уходит.
NEUTRAL_REPLY = "Здравствуйте. Этот бот не ведёт переписку."
