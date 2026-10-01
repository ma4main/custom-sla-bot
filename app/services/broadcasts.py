"""Рассылки: одинаковый текст компании в нескольких чатах за короткое время — не реакция.

Сравнивается тело без префикса интегратора «Имя [домен] пишет:» и с нормализованными
пробелами; порог длины тоже по телу, иначе префикс превращает короткие живые реплики
в «рассылку».
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable

from app.services.attribution import strip_integrator_prefix

MIN_CHATS = 3
MIN_BODY_LENGTH = 60
WINDOW = timedelta(minutes=10)


def broadcast_body(text: str | None) -> str | None:
    body = " ".join(strip_integrator_prefix(text).split())
    return body if len(body) > MIN_BODY_LENGTH else None


def broadcast_message_ids(
    rows: Iterable[tuple[int, int, datetime, str | None]],
    *,
    min_chats: int = MIN_CHATS,
    window: timedelta = WINDOW,
) -> set[int]:
    """Сообщения рассылки: (id, chat_id, sent_at, text) → {id}. Рассылка — текст,
    встречающийся в `min_chats` и более чатах внутри окна `window`; достаточно окон,
    начинающихся в каждом сообщении серии.
    """
    groups: dict[str, list[tuple[datetime, int, int]]] = defaultdict(list)
    for message_id, chat_id, sent_at, text in rows:
        body = broadcast_body(text)
        if body is not None:
            groups[body].append((sent_at, message_id, chat_id))

    found: set[int] = set()
    for series in groups.values():
        if len({chat for _, _, chat in series}) < min_chats:
            continue
        series.sort()
        for index, (start, _, _) in enumerate(series):
            members = [item for item in series[index:] if item[0] - start <= window]
            if len({chat for _, _, chat in members}) >= min_chats:
                found.update(message_id for _, message_id, _ in members)
    return found
