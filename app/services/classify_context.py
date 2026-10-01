"""Причинный контекст для классификации в воркере.

Снимок эпизодов читается один раз, затем для каждого сообщения переигрывается
только строгий префикс его чата — без обращений к базе и провайдеру.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.db.models import BusinessSide, InteractionState
from app.services.ai import OPEN_ITEMS, TAIL_EVENTS, OpenItem, TailEvent
from app.services.episodes import (
    EpisodeReplayInput,
    prepare_episode_replay,
    rebuild_interactions,
)
from app.services.transport_notices import is_integrator_notice
from app.services.verdicts import SOURCE_MODEL, SOURCE_RULE


class ContextTarget(Protocol):
    """Строка очереди; для форумов также нужен ``thread_id``."""

    id: int
    chat_id: int
    sent_at: datetime


Context = tuple[tuple[TailEvent, ...], tuple[OpenItem, ...]]


@dataclass
class ContextBatch:
    replay_input: EpisodeReplayInput
    verdicts: dict[int, tuple] = field(default_factory=dict)
    _messages: dict[int, tuple] = field(init=False, default_factory=dict)
    _keys: dict[int, tuple] = field(init=False, default_factory=dict)
    _by_id: dict[int, object] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        for chat_id, messages in self.replay_input.messages_by_chat.items():
            ordered = tuple(sorted(messages, key=lambda m: (m.sent_at, m.id)))
            self._messages[chat_id] = ordered
            self._keys[chat_id] = tuple((m.sent_at, m.id) for m in ordered)
            self._by_id.update((m.id, m) for m in ordered)

    def record_verdict(
        self, message_id: int, verdict: dict, source: str = SOURCE_MODEL
    ) -> None:
        """Передать успешно сохранённый вердикт в следующие входы.

        В тихом режиме вердикты модели не учитываются (как в движке эпизодов),
        правила — учитываются. Неудачные результаты не записывать.
        """
        if self.replay_input.ai_shadow_mode and source != SOURCE_RULE:
            return
        self.verdicts[message_id] = (
            verdict.get("requires_response"),
            verdict.get("is_substantive"),
            verdict.get("label"),
            verdict.get("answers_request_id"),
        )

    async def context(self, row: ContextTarget) -> Context:
        boundary = (row.sent_at, row.id)
        use_threads = self.replay_input.forum_map.get(row.chat_id, False)
        target = self._by_id.get(row.id)
        thread = getattr(row, "thread_id", getattr(target, "thread_id", None))
        if use_threads and target is None and not hasattr(row, "thread_id"):
            raise ValueError("Для цели в форумном чате нужен thread_id")

        result = await rebuild_interactions(
            None,
            replay_input=self.replay_input,
            chat_ids={row.chat_id},
            before=boundary,
            now=row.sent_at,
            verdict_override=self.verdicts,
            persist=False,
            settle_open=False,
        )
        end = bisect_left(self._keys.get(row.chat_id, ()), boundary)
        previous = self._messages.get(row.chat_id, ())[:end]
        broadcast_ids = set(result["broadcast_ids"])
        events = []
        for message in reversed(previous):
            if use_threads and message.thread_id != thread:
                continue
            if message.id in broadcast_ids or is_integrator_notice(message):
                continue
            if message.business_side not in (BusinessSide.CLIENT, BusinessSide.COMPANY):
                continue
            events.append(
                TailEvent(
                    sent_at=message.sent_at,
                    is_company=message.business_side is BusinessSide.COMPANY,
                    text=message.text,
                    media_kind=message.media_kind,
                )
            )
            if len(events) == TAIL_EVENTS:
                break

        response_ids = set(result["response_required_ids"])
        eligible = {
            item.opened_by_message_id
            for item, members in result["members"]
            if response_ids.intersection(members)
        }
        interactions = sorted(
            (
                item
                for item in result["items"]
                if item.chat_id == row.chat_id
                and item.opened_by_message_id in eligible
                and item.state in (InteractionState.OPEN, InteractionState.REACTED)
                and (not use_threads or item.thread_id == thread)
            ),
            key=lambda item: (item.opened_at, item.opened_by_message_id),
        )
        items = tuple(
            OpenItem(
                message_id=item.opened_by_message_id,
                opened_at=item.opened_at,
                text=self._by_id[item.opened_by_message_id].text,
                first_reaction_at=item.first_reaction_at,
                handoff_at=item.handoff_at,
                substantive_at=item.substantive_at,
                media_kind=self._by_id[item.opened_by_message_id].media_kind,
                has_media=self._by_id[item.opened_by_message_id].has_media,
            )
            for item in interactions[-OPEN_ITEMS:]
        )
        return tuple(reversed(events)), items


async def prepare_contexts(session, pending: list[ContextTarget]) -> ContextBatch:
    """Прочитать один снимок на пачку; цели упорядочены по (sent_at, id).

    Для форумного чата передавать thread_id цели, даже если он None.
    """
    if not pending:
        raise ValueError("prepare_contexts: нужна хотя бы одна цель")
    snapshot = await prepare_episode_replay(
        session,
        chat_ids={row.chat_id for row in pending},
        before=max((row.sent_at, row.id) for row in pending),
    )
    return ContextBatch(snapshot)
