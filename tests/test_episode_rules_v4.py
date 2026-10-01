"""Правила движка v4 и дата перехода `EPISODE_RULES_V4_SINCE`.

Реальный `rebuild_interactions` на изолированной базе; метки и связи заданы
явно как входные данные. Каждое правило проверяется парой «до границы — старое
поведение, после — новое», и держится цепочка тождеств v4 → v3 → v2 → v1.
"""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.db.models import Classification, InteractionState
from app.services.episodes import ENGINE_VERSION, rebuild_interactions
from tests.conftest import requires_db
from tests.test_episode_rules_v2 import CLIENT, COMPANY, T0, _chat, _say, _staff

V4_ON = T0 - timedelta(days=1)


def test_v4_cutover_requires_timezone_and_defaults_off(monkeypatch):
    monkeypatch.delenv("EPISODE_RULES_V4_SINCE", raising=False)
    assert ENGINE_VERSION == 4
    assert Settings(_env_file=None).episode_rules_v4_since is None
    assert Settings(_env_file=None, EPISODE_RULES_V4_SINCE="").episode_rules_v4_since is None
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EPISODE_RULES_V4_SINCE="2026-09-14T10:00:00")


@pytest.fixture
def rules(monkeypatch, request):
    """Версия правил движка: 4 включает v4 с V4_ON, 3 — останавливается на v3."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    version = getattr(request, "param", 4)
    for name, minimum in (
        ("episode_rules_v2_since", 2),
        ("episode_rules_v3_since", 3),
        ("episode_rules_v4_since", 4),
    ):
        monkeypatch.setattr(
            get_settings(), name, V4_ON if version >= minimum else None, raising=False
        )
    return version


async def _items(session, chat, *, hours: float = 3, override=None) -> dict:
    """Обращения чата по открывающему сообщению — без записи в базу."""
    result = await rebuild_interactions(
        session,
        now=T0 + timedelta(hours=hours),
        persist=False,
        verdict_override=override,
    )
    return {
        item.opened_by_message_id: item
        for item in result["items"]
        if item.chat_id == chat.id
    }


# ── Каждая просьба до первой реакции — своё обращение ──────────────────


@requires_db
@pytest.mark.parametrize("rules", [3, 4], indirect=True)
async def test_two_requests_before_reaction_get_own_deadlines(session, rules):
    """Справка численности и штатное расписание в двух сообщениях — два срока."""
    chat = await _chat(session, -100778401)
    first = await _say(session, chat, 1, CLIENT, 0, "Нужна справка о численности", label="request", requires=True)
    second = await _say(session, chat, 2, CLIENT, 1, "И штатное расписание", label="request", requires=True)
    items = await _items(session, chat)
    if rules == 4:
        assert set(items) == {first.id, second.id}
        assert items[first.id].opened_at == first.sent_at
        assert items[second.id].opened_at == second.sent_at
        assert all(item.version == 4 for item in items.values())
        assert all(item.client_messages == 1 for item in items.values())
    else:
        assert set(items) == {first.id}
        assert items[first.id].version == 3 and items[first.id].client_messages == 2


@requires_db
async def test_interaction_opened_before_cutover_keeps_v3(session, rules, monkeypatch):
    """Граница не переписывает прошлое: обращение до неё живёт по v3."""
    monkeypatch.setattr(
        get_settings(), "episode_rules_v4_since", T0 + timedelta(minutes=5), raising=False
    )
    chat = await _chat(session, -100778402)
    first = await _say(session, chat, 1, CLIENT, 0, "Нужна справка о численности", label="request", requires=True)
    await _say(session, chat, 2, CLIENT, 1, "И штатное расписание", label="request", requires=True)
    items = await _items(session, chat)
    assert set(items) == {first.id}
    assert items[first.id].version == 3 and items[first.id].client_messages == 2


# ── Общее подтверждение помощника ──────────────────────────────────────


@requires_db
async def test_common_ack_reacts_to_all_but_linked_handoff_only_to_named_request(session, rules):
    """Общее принятие реагирует обеим; адресная передача открывает один L2."""
    chat = await _chat(session, -100778403)
    manager = await _staff(session, "Помощник")
    first = await _say(session, chat, 1, CLIENT, 0, "Нужна справка о численности", label="request", requires=True)
    second = await _say(session, chat, 2, CLIENT, 1, "И штатное расписание", label="request", requires=True)
    acceptance = await _say(session, chat, 3, COMPANY, 5, "Принято", label="ack", substantive=False, staff=manager)
    handoff = await _say(session, chat, 4, COMPANY, 6, "Штатное расписание передала бухгалтеру", label="handoff", substantive=False, staff=manager)
    await _say(session, chat, 5, COMPANY, 40, "Принято", label="ack", substantive=False, staff=manager)
    items = await _items(session, chat, override={handoff.id: (None, False, "handoff", second.id)})
    assert set(items) == {first.id, second.id}
    assert all(item.first_reaction_message_id == acceptance.id for item in items.values())

    assert items[first.id].handoff_at is None, "веерной передачи нет"
    # Реакция была, передачи нет — помощник ответил сам, ждать больше некого.
    assert items[first.id].state is InteractionState.ANSWERED
    # «Принято» от передавшего сотрудника специалистом не является.
    assert items[second.id].handoff_at == handoff.sent_at
    assert items[second.id].handoff_staff_id == manager.id
    assert items[second.id].substantive_at is None
    assert items[second.id].state is InteractionState.REACTED


@requires_db
async def test_linked_answer_closes_only_its_own_work_but_reacts_to_both(session, rules):
    """Конкретный ответ ЗАКРЫВАЕТ только своё обращение, но первую реакцию снимает с обеих."""
    chat = await _chat(session, -100778404)
    manager = await _staff(session, "Помощник")
    first = await _say(session, chat, 1, CLIENT, 0, "Нужна справка о численности", label="request", requires=True)
    second = await _say(session, chat, 2, CLIENT, 1, "И штатное расписание", label="request", requires=True)
    answer = await _say(session, chat, 3, COMPANY, 5, "Справка о численности во вложении", label="substantive", substantive=True, staff=manager)
    session.add(
        Classification(
            message_id=answer.id,
            model=get_settings().ai_model,
            prompt_version=10,
            source="model",
            label="substantive",
            is_substantive=True,
            answers_request_id=first.id,
        )
    )
    await session.flush()
    items = await _items(session, chat)
    assert items[first.id].state is InteractionState.ANSWERED
    assert items[first.id].substantive_message_id == answer.id
    assert items[second.id].first_reaction_message_id == answer.id, "реплика в чате — реакция всем"
    assert items[second.id].substantive_at is None, "чужая просьба не закрыта"
    # Реакция была, передачи не было — «помощник ответил сам» (settle), а не
    # брошенное обращение.
    assert items[second.id].state is InteractionState.ANSWERED


@requires_db
async def test_answer_without_link_preserves_pending_requests_even_when_only_one(
    session, rules
):
    """Без связи ответ — ОБЩАЯ первая реакция, но не тема и не второй слой.

    Число открытых просьб не доказывает, ЧТО именно закрыто: `substantive_at` без передачи не ставится никому, а ожидание
    первой реакции снимается со всех — реакция сотрудника была.
    """
    chat = await _chat(session, -100778405)
    manager = await _staff(session, "Помощник")
    first = await _say(session, chat, 1, CLIENT, 0, "Нужна справка о численности", label="request", requires=True)
    second = await _say(session, chat, 2, CLIENT, 1, "И штатное расписание", label="request", requires=True)
    answer = await _say(session, chat, 3, COMPANY, 5, "Справка о численности во вложении", label="substantive", substantive=True, staff=manager)
    many = await _items(session, chat)
    assert set(many) == {first.id, second.id}
    assert all(item.first_reaction_message_id == answer.id for item in many.values())
    assert all(item.substantive_at is item.handoff_at is None for item in many.values())

    # Единственность обращения тоже не подтверждает семантическую связь:
    # второй слой без передачи не открыт, закрывать нечего.
    alone = await _chat(session, -100778406)
    only = await _say(session, alone, 1, CLIENT, 0, "Нужна справка о численности", label="request", requires=True)
    single = await _say(session, alone, 2, COMPANY, 5, "Справка во вложении", label="substantive", substantive=True, staff=manager)
    items = await _items(session, alone)
    assert items[only.id].first_reaction_message_id == single.id
    assert items[only.id].substantive_at is items[only.id].handoff_at is None


# ── Дополнение и поправка ──────────────────────────────────────────────


@requires_db
@pytest.mark.parametrize("rules", [3, 4], indirect=True)
async def test_correction_before_reaction_shifts_the_start(session, rules):
    """«Пропустила цифру» до первой реакции переносит начало срока."""
    chat = await _chat(session, -100778407)
    request = await _say(session, chat, 1, CLIENT, 0, "Оплатите 12 000 за июль", label="request", requires=True)
    fix = await _say(session, chat, 2, CLIENT, 20, "Пропустила цифру, правильно 12 500", label="correction", requires=False)
    items = await _items(session, chat)
    assert set(items) == {request.id}
    expected = fix.sent_at if rules == 4 else request.sent_at
    assert items[request.id].opened_at == expected
    assert items[request.id].client_messages == 2, "поправка своего дела не открывает"


@requires_db
async def test_correction_after_reaction_behaves_like_addition(session, rules):
    """После первой реакции поправка срок не двигает: отвечать уже начали."""
    chat = await _chat(session, -100778408)
    manager = await _staff(session, "Помощник")
    request = await _say(session, chat, 1, CLIENT, 0, "Оплатите 12 000 за июль", label="request", requires=True)
    await _say(session, chat, 2, COMPANY, 5, "Приняла", label="ack", substantive=False, staff=manager)
    await _say(session, chat, 3, CLIENT, 20, "Пропустила цифру, правильно 12 500", label="correction", requires=False)
    items = await _items(session, chat)
    assert items[request.id].opened_at == request.sent_at
    assert items[request.id].client_messages == 2


@requires_db
@pytest.mark.parametrize("rules", [3, 4], indirect=True)
async def test_addition_without_open_request_is_a_request(session, rules):
    """Дополнять нечего — значит, это новая просьба со своим сроком."""
    chat = await _chat(session, -100778409)
    lonely = await _say(session, chat, 1, CLIENT, 0, "Вот ещё документы", label="addition", requires=False)
    items = await _items(session, chat)
    assert set(items) == {lonely.id}
    if rules == 4:
        assert items[lonely.id].state is InteractionState.OPEN
        assert items[lonely.id].sla_breached is None
    else:
        assert items[lonely.id].state is InteractionState.NO_RESPONSE_NEEDED


@requires_db
async def test_addition_joins_the_open_request_without_shift(session, rules):
    """«И вот этот договор» присоединяется к открытой просьбе как есть."""
    chat = await _chat(session, -100778410)
    request = await _say(session, chat, 1, CLIENT, 0, "Пришлите закрывающие за август", label="request", requires=True)
    await _say(session, chat, 2, CLIENT, 20, "И вот этот договор", label="addition", requires=False)
    items = await _items(session, chat)
    assert set(items) == {request.id}
    assert items[request.id].opened_at == request.sent_at
    assert items[request.id].client_messages == 2


# ── Связь неизвестна ───────────────────────────────────────────────────


async def _two_specialist_waits(session, chat_id: int):
    """Два независимых ожидания специалиста: просьба, передача, ещё просьба,
    ещё передача. Веером второй слой не открывается, поэтому каждая
    передача достаётся своему обращению."""
    chat = await _chat(session, chat_id)
    manager = await _staff(session, "Помощник")
    specialist = await _staff(session, "Бухгалтер")
    first = await _say(session, chat, 1, CLIENT, 0, "Налоги по такси?", label="question", requires=True)
    handoff_one = await _say(session, chat, 2, COMPANY, 2, "Передала бухгалтеру", label="handoff", substantive=False, staff=manager)
    second = await _say(session, chat, 3, CLIENT, 30, "А по маркетплейсам?", label="question", requires=True)
    handoff_two = await _say(session, chat, 4, COMPANY, 32, "Этот вопрос тоже передала", label="handoff", substantive=False, staff=manager)
    # Эти тесты начинают с двух доказанных передач. Отсутствие связи у
    # последующего ответа проверяется отдельно от угадывания самой передачи.
    for handoff, target in ((handoff_one, first), (handoff_two, second)):
        session.add(Classification(
            message_id=handoff.id, model=get_settings().ai_model, prompt_version=13,
            source="model", label="handoff", is_substantive=False,
            answers_request_id=target.id,
        ))
    await session.flush()
    return chat, manager, specialist, first, second, handoff_one, handoff_two


@requires_db
async def test_specialist_contact_without_link_preserves_both_waits(session, rules):
    """В v4 неизвестная тема не закрывает ни одно ожидание второго слоя."""
    chat, _, specialist, first, second, *_ = await _two_specialist_waits(session, -100778411)
    reply = await _say(session, chat, 5, COMPANY, 60, "Подготовлю сегодня", label="substantive", substantive=True, staff=specialist)
    items = await _items(session, chat)
    assert items[first.id].handoff_at is not None and items[second.id].handoff_at is not None
    assert items[first.id].substantive_at is None, "первое ожидание живо"
    assert items[second.id].substantive_at is None
    assert items[first.id].state is items[second.id].state is InteractionState.REACTED


@requires_db
async def test_known_link_closes_the_named_wait(session, rules):
    """Известная связь закрывает именно указанное ожидание."""
    chat, _, specialist, first, second, one, two = await _two_specialist_waits(
        session, -100778412
    )
    reply = await _say(session, chat, 5, COMPANY, 60, "По такси ответ такой", label="substantive", substantive=True, staff=specialist)
    items = await _items(
        session,
        chat,
        override={
            first.id: (True, None, "question"),
            second.id: (True, None, "question"),
            one.id: (None, False, "handoff", first.id),
            two.id: (None, False, "handoff", second.id),
            reply.id: (None, True, "substantive", first.id),
        },
    )
    assert items[first.id].substantive_message_id == reply.id
    assert items[first.id].state is InteractionState.ANSWERED
    assert items[second.id].substantive_at is None
    assert items[second.id].state is InteractionState.REACTED


# ── Тождества версий ───────────────────────────────────────────────────


@requires_db
async def test_disabled_v4_repeats_v3_field_by_field(session, monkeypatch):
    """v4 выключена → результат в точности как у v3, включая состав обращений.

    Сценарий нарочно задевает все изменённые правила: серия просьб до
    реакции, общее подтверждение, передача, контакт специалиста, поправка.
    """
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    chat = await _chat(session, -100778413)
    manager = await _staff(session, "Помощник")
    specialist = await _staff(session, "Бухгалтер")
    await _say(session, chat, 1, CLIENT, 0, "Нужна справка о численности", label="request", requires=True)
    await _say(session, chat, 2, CLIENT, 1, "И штатное расписание", label="request", requires=True)
    await _say(session, chat, 3, COMPANY, 5, "Передала бухгалтеру", label="handoff", substantive=False, staff=manager)
    await _say(session, chat, 4, CLIENT, 30, "Ещё нужен акт сверки", label="request", requires=True)
    await _say(session, chat, 5, COMPANY, 40, "Готовлю", label="substantive", substantive=True, staff=specialist)
    await _say(session, chat, 6, CLIENT, 50, "Спасибо", label="ack", requires=False)

    snapshots = {}
    fields = (
        "opened_at", "opened_by_message_id", "last_client_at", "client_messages",
        "first_reaction_at", "first_reaction_message_id", "handoff_at",
        "substantive_at", "substantive_message_id", "state",
        "ttfr_seconds", "ttfa_seconds", "sla_breached", "substantive_breached",
    )
    for version in (1, 2, 3, 4):
        for name, minimum in (
            ("episode_rules_v2_since", 2),
            ("episode_rules_v3_since", 3),
            ("episode_rules_v4_since", 4),
        ):
            monkeypatch.setattr(
                get_settings(), name, V4_ON if version >= minimum else None, raising=False
            )
        items = await _items(session, chat, hours=4)
        snapshots[version] = {
            opener: tuple(getattr(item, field) for field in fields)
            for opener, item in items.items()
        }

    # v4 выключена → ровно результат v3; v3 выключена → v2; обе → v1.
    # На этом сценарии (без уведомлений, рассылок и вложений) правила v2
    # ничего не меняют, поэтому v1 и v2 обязаны совпасть тоже — а вот v3
    # и v4 обязаны отличаться, иначе тест ничего не проверяет.
    assert snapshots[1] == snapshots[2], "v3 и v4 выключены — поведение v1"
    assert snapshots[3] != snapshots[2], "v3 должна разделять просьбу при передаче"
    assert snapshots[4] != snapshots[3], "v4 должна разделять просьбы до реакции"
    # v1/v2: одно обращение на все три просьбы плюс отдельное «спасибо»;
    # v3: просьба во время передачи отделилась; v4: отделились и первые две.
    assert len(snapshots[1]) == 2 and len(snapshots[3]) == 2 and len(snapshots[4]) == 3


@requires_db
@pytest.mark.parametrize("version", [1, 2, 3])
async def test_older_versions_are_unchanged_by_v4_code(session, monkeypatch, version):
    """Выключенная v4 (и v3, и v2) оставляет прежний единственный эпизод."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    for name, minimum in (
        ("episode_rules_v2_since", 2),
        ("episode_rules_v3_since", 3),
        ("episode_rules_v4_since", 4),
    ):
        monkeypatch.setattr(
            get_settings(), name, V4_ON if version >= minimum else None, raising=False
        )
    chat = await _chat(session, -100778414 - version)
    first = await _say(session, chat, 1, CLIENT, 0, "Нужна справка о численности", label="request", requires=True)
    await _say(session, chat, 2, CLIENT, 1, "И штатное расписание", label="request", requires=True)
    items = await _items(session, chat)
    assert set(items) == {first.id}
    assert items[first.id].version == version
    assert items[first.id].client_messages == 2
