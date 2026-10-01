"""Причинный вход движка и контрпримеры повторной передачи на синтетических текстах."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json
from types import MappingProxyType

import pytest

from app.db.models import BusinessSide, InteractionState, TransportActorKind
from app.services.episodes import (
    EpisodeMessage, EpisodeReplayInput, expiry_moment, prepare_episode_replay, rebuild_interactions,
)
from tests.conftest import requires_db

T0 = datetime(2026, 9, 14, 7, 30, tzinfo=timezone.utc)


def message(mid, minute, side=BusinessSide.CLIENT, *, chat=1, text="Synthetic task", thread=None):
    return EpisodeMessage(mid, chat, thread, T0 + timedelta(minutes=minute), side,
                          text, False, None, TransportActorKind.HUMAN_USER)


def snapshot(messages, verdicts=None, *, version=4, broadcasts=(), settings=None, forum=False):
    sections = settings or (
        {"timezone": "Europe/Moscow", "start": "09:00", "end": "18:00"},
        {}, {},
    )
    by_chat = {}
    for item in messages:
        by_chat.setdefault(item.chat_id, []).append(item)
    return EpisodeReplayInput(
        tuple(by_chat), MappingProxyType({key: tuple(value) for key, value in by_chat.items()}),
        MappingProxyType({key: forum for key in by_chat}), MappingProxyType({2: 10, 5: 10, 6: 20}),
        frozenset(), MappingProxyType(verdicts or {}), tuple(broadcasts),
        *(json.dumps(section) for section in sections),
        tuple(T0 - timedelta(days=1) if version >= v else None for v in (2, 3, 4)),
        False,
    )


async def replay(data, *, before=None, override=None, settle=False, chat_ids=None):
    result = await rebuild_interactions(
        None, persist=False, replay_input=data, chat_ids=chat_ids,
        before=before, now=before[0] if before else T0 + timedelta(hours=2),
        verdict_override=override, settle_open=settle,
    )
    return {item.opened_by_message_id: item for item in result["items"]}


@pytest.mark.parametrize("version", [1, 2, 3, 4])
async def test_prefix_is_exclusive_at_equal_timestamp_and_cannot_see_answer(version):
    data = snapshot([
        message(1, 0), message(2, 0),
        message(3, 0, BusinessSide.COMPANY, text="Synthetic completed response"),
        message(4, 1, chat=2),
    ], {1: (True, None, "request", None), 2: (True, None, "request", None),
        3: (None, True, "substantive", 1)}, version=version)
    items = await replay(data, before=(T0, 3), chat_ids={1})
    assert set(items) == ({1, 2} if version == 4 else {1})
    assert all(item.substantive_at is None and item.first_reaction_at is None for item in items.values())
    assert all(item.chat_id == 1 and item.version == version for item in items.values())
    after = await replay(data, before=(T0 + timedelta(minutes=1), 4), chat_ids={1})
    assert after[1].substantive_message_id == 3


async def test_prefix_receives_updated_verdict_without_mutating_snapshot_or_previous_result():
    data = snapshot([message(1, 0), message(2, 1)], {1: (False, None, "info", None)})
    before = (T0 + timedelta(minutes=1), 2)
    earlier = await replay(data, before=before)
    updated = await replay(data, before=before, override={1: (True, None, "request", None)})
    assert data.verdicts[1] == (False, None, "info", None)
    assert updated[1].state is InteractionState.OPEN
    assert updated[1] is not earlier[1]
    with pytest.raises(TypeError):
        data.messages_by_chat[1] = ()
    with pytest.raises(FrozenInstanceError):
        data.messages_by_chat[1][0].text = "changed"


async def test_effective_request_metadata_excludes_info_and_includes_promoted_correction():
    info = snapshot([message(1, 0)], {1: (False, None, "info", None)})
    result = await rebuild_interactions(None, persist=False, replay_input=info, now=T0,
                                        before=(T0, 2), settle_open=False)
    assert result["response_required_ids"] == []
    correction = snapshot([message(1, 0)], {1: (False, None, "correction", None)})
    result = await rebuild_interactions(None, persist=False, replay_input=correction, now=T0,
                                        before=(T0, 2), settle_open=False)
    assert result["response_required_ids"] == [1]


async def test_snapshot_rules_survive_later_runtime_setting_changes(monkeypatch):
    from app.config import get_settings

    data = snapshot([message(1, 0), message(2, 1)],
                    {1: (True, None, "request", None), 2: (True, None, "request", None)})
    for version in (2, 3, 4):
        monkeypatch.setattr(get_settings(), f"episode_rules_v{version}_since", None)
    items = await replay(data)
    assert set(items) == {1, 2}
    assert all(item.version == 4 for item in items.values())


async def test_future_broadcast_does_not_retroactively_hide_prior_reaction():
    body = "Synthetic company notice with enough characters to qualify for the broadcast detector."
    rows = ((2, 1, T0 + timedelta(minutes=1), body),
            (3, 2, T0 + timedelta(minutes=2), body),
            (4, 3, T0 + timedelta(minutes=3), body))
    data = snapshot([message(1, 0), message(2, 1, BusinessSide.COMPANY, text=body)],
                    {1: (True, None, "request", None), 2: (None, False, "ack", 1)}, broadcasts=rows)
    before = (T0 + timedelta(minutes=2), 3)
    items = await replay(data, before=before)
    assert items[1].first_reaction_message_id == 2
    later = await replay(data, before=(T0 + timedelta(minutes=4), 5))
    assert later[1].first_reaction_at is None
    result = await rebuild_interactions(None, persist=False, replay_input=data, before=before,
                                        now=before[0], settle_open=False)
    assert result["broadcast_ids"] == []
    later_boundary = (T0 + timedelta(minutes=4), 5)
    result = await rebuild_interactions(None, persist=False, replay_input=data, before=later_boundary,
                                        now=later_boundary[0], settle_open=False)
    assert result["broadcast_ids"] == [2, 3, 4]


async def test_broadcast_metadata_filters_tail_with_rules_off_without_changing_v1_reaction():
    body = "Synthetic company notice with enough characters to qualify for the broadcast detector."
    rows = tuple((mid, chat, T0 + timedelta(minutes=minute), body)
                 for mid, chat, minute in ((2, 1, 1), (3, 2, 2), (4, 3, 3)))
    data = snapshot([message(1, 0), message(2, 1, BusinessSide.COMPANY, text=body)],
                    {1: (True, None, "request", None), 2: (None, False, "ack", None)},
                    broadcasts=rows, version=1)
    boundary = (T0 + timedelta(minutes=4), 5)
    result = await rebuild_interactions(None, persist=False, replay_input=data, before=boundary,
                                        now=boundary[0], settle_open=False)
    assert result["broadcast_ids"] == [2, 3, 4]
    assert result["items"][0].first_reaction_message_id == 2


@pytest.mark.parametrize("version", [1, 2, 3, 4])
async def test_unknown_photo_after_ack_gets_new_reaction_only_in_v4(version):
    """Фото → «принято» → незапрошенное фото: новая реакция нужна только в v4."""
    first = replace(message(1, 0, text=None), has_media=True, media_kind="photo")
    second = replace(message(3, 1, text=None), has_media=True, media_kind="photo")
    data = snapshot([first, message(2, .25, BusinessSide.COMPANY, text="Принято"), second],
                    {2: (None, False, "ack", None)}, version=version)
    items = await replay(data, before=(T0 + timedelta(minutes=2), 4))
    assert items[1].first_reaction_message_id == 2
    if version == 4:
        assert set(items) == {1, 3}
        assert items[1].state is InteractionState.ANSWERED
        assert items[3].opened_at == second.sent_at
        assert items[3].first_reaction_at is None
        assert items[3].state is InteractionState.OPEN
    else:
        assert set(items) == {1}
        assert items[1].client_messages == 2


async def test_unknown_files_before_first_reaction_still_form_one_batch():
    files = [replace(message(mid, mid, text=None), has_media=True, media_kind="document")
             for mid in (1, 2)]
    items = await replay(snapshot(files), before=(T0 + timedelta(minutes=3), 3))
    assert set(items) == {1}
    assert items[1].client_messages == 2
    assert items[1].first_reaction_at is None


async def test_requested_photo_burst_after_ack_remains_answer_without_new_timer():
    first = replace(message(2, 1, text=None), has_media=True, media_kind="photo")
    second = replace(message(4, 2, text=None), has_media=True, media_kind="photo")
    data = snapshot([message(1, 0, BusinessSide.COMPANY, text="Send the requested scan?"),
                     first, message(3, 1.5, BusinessSide.COMPANY, text="Received scan"), second],
                    {1: (None, False, "question", None), 3: (None, False, "ack", None)})
    boundary = (T0 + timedelta(minutes=3), 5)
    result = await rebuild_interactions(None, persist=False, replay_input=data,
                                        before=boundary, now=boundary[0], settle_open=False)
    assert result["answers"] == [2, 4]
    assert result["response_required_ids"] == []
    assert all(item.first_reaction_at is None for item in result["items"])


async def test_explicit_answer_photo_after_ack_does_not_open_new_request():
    photo = replace(message(3, 1, text=None), has_media=True, media_kind="photo")
    data = snapshot([message(1, 0), message(2, .25, BusinessSide.COMPANY), photo],
                    {1: (True, None, "request", None), 2: (None, False, "ack", None),
                     3: (False, None, "answer", None)})
    items = await replay(data)
    assert set(items) == {1}
    assert items[1].client_messages == 2


@pytest.mark.parametrize("label", ["addition", "correction"])
@pytest.mark.parametrize("same_timestamp", [False, True])
async def test_unknown_material_accompanying_intervening_client_update_keeps_batch(label, same_timestamp):
    """Неразобранное фото после поправки клиента своего «принято» ещё не получило."""
    update_minute = 0 if same_timestamp else 1
    ack_minute = 0 if same_timestamp else .25
    photo = replace(message(4, update_minute, text=None), has_media=True, media_kind="photo")
    data = snapshot([message(1, 0), message(2, ack_minute, BusinessSide.COMPANY, text="Принято"),
                     message(3, update_minute, text="Updated material for the same work"), photo],
                    {1: (True, None, "request", None), 2: (None, False, "ack", None),
                     3: (False, None, label, None)})
    items = await replay(data)
    assert set(items) == {1}
    assert items[1].client_messages == 3
    assert items[1].first_reaction_message_id == 2


async def test_prefix_avoids_final_settlement_and_uses_historical_settings():
    sections = (
        {"timezone": "Europe/Moscow"}, {},
        {"threshold_minutes": 60, "since": (T0 + timedelta(minutes=10)).isoformat(),
         "history": [{"since": None, "threshold_minutes": 30}]},
    )
    data = snapshot([message(1, 0), message(2, 40, BusinessSide.COMPANY, text="Принято")],
                    {1: (True, None, "request", None), 2: (None, False, "ack", None)}, settings=sections)
    causal = await replay(data)
    assert causal[1].state is InteractionState.REACTED
    assert causal[1].sla_breached is True
    normal_final = await replay(data, settle=True)
    assert normal_final[1].state is InteractionState.ANSWERED


async def test_prefix_expires_elapsed_wait_but_preserves_request_through_expiry_boundary():
    data = snapshot([message(1, 0)], {1: (True, None, "request", None)})
    active = await replay(data, before=(T0 + timedelta(hours=2), 99))
    deadline = expiry_moment(active[1], json.loads(data.calendar_json),
                             sla_seconds=1800, substantive_seconds=3600,
                             wait_reaction_seconds=86400, wait_specialist_seconds=604800)
    items = await replay(data, before=(deadline, 99))
    assert items[1].state is InteractionState.OPEN
    elapsed = await replay(data, before=(deadline + timedelta(seconds=1), 99))
    assert elapsed[1].state is InteractionState.ABANDONED
    stale = await replay(data, before=(T0 + timedelta(days=60), 99))
    assert stale[1].state is InteractionState.ABANDONED


@pytest.mark.parametrize("kwargs", [{"persist": True}, {"persist": False, "now": None},
                                    {"persist": False, "now": T0 + timedelta(minutes=1)}])
async def test_prefix_requires_read_only_mode_and_exact_boundary_clock(kwargs):
    data = snapshot([message(1, 0)])
    with pytest.raises(ValueError):
        await rebuild_interactions(None, replay_input=data, before=(T0, 2), **kwargs,
                                   **({} if "now" in kwargs else {"now": T0}))


async def test_snapshot_rejects_later_boundary():
    data = replace(snapshot([message(1, 0)]), before=(T0, 2))
    with pytest.raises(ValueError, match="позже подготовленного"):
        await replay(data, before=(T0 + timedelta(minutes=1), 3))
    with pytest.raises(ValueError, match="Снимку с границей"):
        await replay(data)


async def test_forum_replay_preserves_separate_threads_and_excludes_future_in_each():
    data = snapshot([
        message(1, 0, thread=10), message(2, 1, thread=20),
        message(3, 2, BusinessSide.COMPANY, thread=20),
        message(4, 3, BusinessSide.COMPANY, thread=10),
    ], {1: (True, None, "request", None), 2: (True, None, "request", None),
        3: (None, True, "substantive", 2), 4: (None, True, "substantive", 1)}, forum=True)
    items = await replay(data, before=(T0 + timedelta(minutes=3), 4))
    assert items[1].thread_id == 10 and items[1].substantive_at is None
    assert items[2].thread_id == 20 and items[2].substantive_message_id == 3


@pytest.mark.parametrize("new_count", [0, 1, 2])
async def test_repeat_explicit_old_handoff_preserves_identity_deadline_and_new_request(new_count):
    """Настоящий повтор про старую тему не перенаправляется на более новую задачу.

    Те же флаги движка описывают и ошибочную ссылку на старую тему: какую
    тему имеет в виду фраза, по ним не установить. Проверяется ВТОРОЙ слой —
    он адресный. Реплика сотрудника в чате снимает первый срок со всех
    открытых работ независимо от `answers_request_id`, поэтому новые
    работы становятся REACTED по сообщению 5, а срок специалиста остаётся
    только у старой темы.
    """
    messages = [message(1, 0), message(2, 1, BusinessSide.COMPANY, text="Handed old task to specialist")]
    verdicts = {1: (True, None, "request", None), 2: (None, False, "handoff", 1)}
    for index in range(new_count):
        mid = 3 + index
        messages.append(message(mid, mid))
        verdicts[mid] = (True, None, "request", None)
    messages.extend([message(5, 5, BusinessSide.COMPANY, text="Reminded specialist about old task"),
                     message(6, 6, BusinessSide.COMPANY, text="Old task completed")])
    verdicts.update({5: (None, False, "handoff", 1), 6: (None, True, "substantive", 1)})
    items = await replay(snapshot(messages, verdicts))
    assert items[1].handoff_at == T0 + timedelta(minutes=1)
    assert items[1].first_reaction_message_id == 2
    assert items[1].substantive_message_id == 6
    for mid in range(3, 3 + new_count):
        assert items[mid].first_reaction_message_id == 5
        assert items[mid].handoff_at is None
        assert items[mid].substantive_at is None
        # Реакция без передачи закрывает обращение как «помощник ответил сам»
        # (`v4_settle_finished`); последнее открытое остаётся ждать продолжения.
        assert items[mid].state is (
            InteractionState.REACTED if mid == 2 + new_count else InteractionState.ANSWERED
        )


async def test_corrected_handoff_link_reacts_and_starts_second_deadline_on_new_request():
    data = snapshot([message(1, 0), message(2, 1, BusinessSide.COMPANY), message(3, 3),
                     message(5, 5, BusinessSide.COMPANY)],
                    {1: (True, None, "request", None), 2: (None, False, "handoff", 1),
                     3: (True, None, "request", None), 5: (None, False, "handoff", 1)})
    wrong = await replay(data)
    # Ошибка ссылки видна ТОЛЬКО во втором слое: первую реакцию новая работа
    # получает от реплики в чате, а срока специалиста у неё нет,
    # потому что передача ушла на старую тему.
    assert wrong[3].first_reaction_message_id == 5
    assert wrong[3].handoff_at is None
    corrected = await replay(data, override={5: (None, False, "handoff", 3)})
    assert corrected[3].first_reaction_message_id == 5
    assert corrected[3].handoff_at == T0 + timedelta(minutes=5)
    assert corrected[1].handoff_at == T0 + timedelta(minutes=1)
    unknown = await replay(data, override={5: (None, False, "handoff", None)})
    # Передача без темы — ОБЩАЯ первая реакция. Угадывать не нужно: открытая работа без передачи
    # одна, и передача — её первая реакция, поэтому срок специалиста открывается ей.
    assert unknown[3].first_reaction_message_id == 5
    assert unknown[3].handoff_at == T0 + timedelta(minutes=5)
    assert unknown[3].state is InteractionState.REACTED
    assert unknown[1].handoff_at == T0 + timedelta(minutes=1)


@requires_db
@pytest.mark.parametrize("version", [1, 2, 3, 4])
async def test_prepared_replay_matches_normal_dry_run_and_preserves_session(session, monkeypatch, version):
    from app.config import get_settings
    from tests.test_episode_rules_v2 import CLIENT, COMPANY, _chat, _say

    for minimum in (2, 3, 4):
        monkeypatch.setattr(get_settings(), f"episode_rules_v{minimum}_since",
                            T0 - timedelta(days=60) if version >= minimum else None)
    chat = await _chat(session, -100778490 - version)
    await _say(session, chat, 1, CLIENT, 0, "Synthetic first task", label="request", requires=True)
    await _say(session, chat, 2, CLIENT, 1, "Synthetic second task", label="request", requires=True)
    await _say(session, chat, 3, COMPANY, 5, "Synthetic handoff", label="handoff", substantive=False)
    now = T0 + timedelta(hours=2)
    normal = await rebuild_interactions(session, persist=False, now=now)
    before_new, before_dirty, before_deleted = set(session.new), set(session.dirty), set(session.deleted)
    prepared = await prepare_episode_replay(session, chat_ids={chat.id})
    actual = await rebuild_interactions(None, persist=False, now=now, replay_input=prepared)
    assert (set(session.new), set(session.dirty), set(session.deleted)) == (before_new, before_dirty, before_deleted)
    fields = ("opened_by_message_id", "version", "opened_at", "state", "client_messages",
              "first_reaction_at", "first_reaction_message_id", "handoff_at", "substantive_at",
              "sla_breached", "substantive_breached")
    expected = [tuple(getattr(item, field) for field in fields) for item in normal["items"] if item.chat_id == chat.id]
    assert [tuple(getattr(item, field) for field in fields) for item in actual["items"]] == expected
