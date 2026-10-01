"""Первое обращение чата после включения v4 создаётся по v4 и попадает
в `open_v4` по своей версии, а не по режиму чата, — иначе оно осталось бы
сиротой без реакций и навсегда OPEN.
"""

from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType

from app.db.models import InteractionState
from app.services.episodes import rebuild_interactions
from tests.test_classify_context_v11 import COMPANY, REQUEST, T0, batch, message

ACK = (None, False, "ack", None)
SUBSTANTIVE = (None, True, "substantive", None)

# Граница включения v4 — между первой и второй просьбой клиента.
V4_SINCE = T0 + timedelta(seconds=100)


async def replay(messages, verdicts, *, v4_since=V4_SINCE, after=timedelta(hours=3)):
    """Пересборка с ГРАНИЦЕЙ версий внутри чата.

    `rules_since` — тройка (v2, v3, v4). v2 включена задолго до T0, v4 —
    в момент `v4_since`, поэтому сообщения по обе стороны границы попадают
    в разные ветки движка.
    """
    snapshot = batch(messages, verdicts).replay_input
    snapshot = replace(
        snapshot, attribution_map=MappingProxyType({}),
        rules_since=(T0 - timedelta(days=1), None, v4_since),
    )
    result = await rebuild_interactions(
        None, replay_input=snapshot, persist=False, now=T0 + after, settle_open=False,
    )
    return {item.opened_by_message_id: item for item in result["items"]}, result


async def test_first_v4_work_of_a_chat_gets_the_staff_reaction():
    """v2-обращение закрыто новой просьбой уже по v4.

    Просьба #12 приходит после границы, закрывает старое v2-обращение
    («помощник ответил сам») и открывает своё — версии 4. Реплика
    сотрудника через пять минут обязана стать его первой реакцией.
    """
    messages = [
        message(10, seconds=0, text="Нужна копия декларации 2025"),
        message(11, COMPANY, seconds=60, text="Принято, посмотрю"),
        message(12, seconds=300, text="Выставьте, пожалуйста, счет на оплату"),
        message(13, COMPANY, seconds=600, text="Счет выставлен, отправила на почту"),
    ]
    items, _ = await replay(
        messages, {10: REQUEST, 11: ACK, 12: REQUEST, 13: SUBSTANTIVE},
    )
    assert items[12].version == 4
    assert items[12].first_reaction_at == messages[3].sent_at
    assert items[12].first_reaction_message_id == 13
    assert items[12].sla_breached is False
    assert items[12].state is not InteractionState.OPEN


async def test_the_orphan_never_stays_open_without_a_reaction():
    """Та же граница, но реплика сотрудника ОДНА — и она обязана дойти."""
    messages = [
        message(10, seconds=0, text="Нужна копия декларации 2025"),
        message(11, COMPANY, seconds=60, text="Принято, посмотрю"),
        message(12, seconds=300, text="Выставьте, пожалуйста, счет на оплату"),
        message(13, COMPANY, seconds=420, text="Приняли в работу"),
    ]
    items, _ = await replay(messages, {10: REQUEST, 11: ACK, 12: REQUEST, 13: ACK})
    assert items[12].first_reaction_at is not None
    assert items[12].state is not InteractionState.OPEN


async def test_the_old_v2_work_still_lives_by_its_own_rules():
    """Старое обращение границей не тронуто: своя версия, свой ответ.

    Вторая реплика клиента присоединяется к нему по v2 (в v4 она открыла бы
    своё дело), и закрывает его ответ компании уже после границы.
    """
    messages = [
        message(10, seconds=0, text="Нужна копия декларации 2025"),
        message(11, seconds=50, text="И ещё справку по форме 2-НДФЛ"),
        message(12, COMPANY, seconds=400, text="Декларация и справка во вложении"),
    ]
    items, _ = await replay(messages, {10: REQUEST, 11: REQUEST, 12: SUBSTANTIVE})
    assert set(items) == {10}
    assert items[10].version == 2
    assert items[10].client_messages == 2
    assert items[10].state is InteractionState.ANSWERED


async def test_a_chat_without_an_older_work_is_unchanged():
    """Чат, где к моменту включения v4 открытых обращений не было."""
    messages = [
        message(10, seconds=200, text="Выставьте, пожалуйста, счет на оплату"),
        message(11, COMPANY, seconds=500, text="Счет выставлен, отправила на почту"),
    ]
    items, _ = await replay(messages, {10: REQUEST, 11: SUBSTANTIVE})
    assert set(items) == {10}
    assert items[10].version == 4
    assert items[10].first_reaction_at == messages[1].sent_at
    assert items[10].sla_breached is False


async def test_two_works_after_the_boundary_both_get_the_fan_out():
    """4.2 на границе: обе просьбы после неё открыты, реплика без reply — обеим."""
    messages = [
        message(10, seconds=0, text="Нужна копия декларации 2025"),
        message(11, COMPANY, seconds=60, text="Принято, посмотрю"),
        message(12, seconds=300, text="Выставьте, пожалуйста, счет на оплату"),
        message(13, seconds=360, text="И подготовьте акт сверки с ООО «Северный ветер»"),
        message(14, COMPANY, seconds=600, text="Счет и акт отправила на почту"),
    ]
    items, _ = await replay(
        messages, {10: REQUEST, 11: ACK, 12: REQUEST, 13: REQUEST, 14: SUBSTANTIVE},
    )
    for opener in (12, 13):
        assert items[opener].version == 4
        assert items[opener].first_reaction_message_id == 14
        assert items[opener].sla_breached is False
