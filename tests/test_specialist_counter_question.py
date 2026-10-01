"""Встречный вопрос сотрудника выполняет ожидание специалиста (только v4).

После встречного вопроса ход у клиента, а за клиентом движок не следит —
третьего слоя нет. Закреплено:

  * адресуется вопрос ССЫЛКОЙ модели — второй слой остаётся строгим.
    Со ссылкой на работу, ждущую специалиста, автор не важен — хоть тот же
    менеджер, что и передал;
  * БЕЗ ссылки закрывает только прежний путь — единственное ожидание и
    автор, отличный от передавшего. Вопрос самого передавшего без ссылки
    ожидание НЕ снимает (тема не названа), двух ожиданий движок не
    разбирает;
  * признак встречного вопроса — тот же `asks_client`, которым v2 передаёт
    ход клиенту: метка `question` ИЛИ реплика, кончающаяся «?». Одной метки
    мало: передача с вопросом размечается `handoff`;
  * обращение при этом НЕ закрывается — ход за клиентом, как в v1–v3;
  * v1–v3 правкой не задеты.

Метки и ссылки здесь — заданный вход, а не предсказание модели.
"""

import pytest

from app.db.models import InteractionState
from tests.test_classify_context_v11 import COMPANY, REQUEST, message
from tests.test_engine_v4_replay import ACK, HANDOFF, replay

QUESTION = "question"
# Передавший менеджер и специалист, вышедший на связь.
IRINA, TAMARA = 10, 20


# ── Ссылка есть: автор не важен ──────────────────────────────────────────

async def test_handoff_that_asks_the_client_back_never_starts_a_wait():
    """Передача и встречный вопрос в одной реплике.

    `handoff_at` проставляется, но тем же временем встаёт `substantive_at`,
    поэтому срок второго слоя не нарушается.
    """
    messages = [message(863, seconds=0, text="Здравствуйте, закройте пожалуйста лишние сеансы"),
                message(870, COMPANY, seconds=304,
                        text="Здравствуйте, передала ваш запрос, подскажите удалось войти?")]
    items, _ = await replay(messages, {863: REQUEST, 870: (None, False, HANDOFF, 863)},
                            staff={870: IRINA})
    item = items[863]
    assert item.handoff_at == messages[1].sent_at
    assert item.substantive_message_id == 870
    assert item.substantive_at == messages[1].sent_at
    assert item.substantive_breached is False


async def test_linked_counter_question_after_a_handoff_closes_the_wait():
    """«Подскажите, удалось войти?» отдельной репликой после передачи."""
    messages = [message(1, text="Закройте неиспользуемые сеансы"),
                message(2, COMPANY, seconds=300, text="Передала ваш запрос"),
                message(3, COMPANY, seconds=600, text="Подскажите, удалось войти?")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1),
                                       3: (None, False, QUESTION, 1)},
                            staff={2: IRINA, 3: IRINA})
    assert items[1].handoff_at == messages[1].sent_at
    assert items[1].substantive_message_id == 3


async def test_the_question_label_alone_is_enough_without_a_question_mark():
    """Метка `question` работает и без «?» — второй признак того же `asks_client`."""
    messages = [message(1, text="Закройте неиспользуемые сеансы"),
                message(2, COMPANY, seconds=300, text="Передала ваш запрос"),
                message(3, COMPANY, seconds=600, text="Уточните, пожалуйста, логин")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1),
                                       3: (None, False, QUESTION, 1)},
                            staff={2: IRINA, 3: IRINA})
    assert items[1].substantive_message_id == 3


async def test_a_counter_question_does_not_close_the_work_itself():
    """Ход за клиентом: ожидание выполнено, обращение остаётся открытым (v1–v3)."""
    messages = [message(1, text="Закройте неиспользуемые сеансы"),
                message(2, COMPANY, seconds=300, text="Передала ваш запрос, удалось войти?")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1)},
                            staff={2: IRINA})
    assert items[1].substantive_message_id == 2
    assert items[1].state is not InteractionState.ANSWERED


async def test_a_link_to_a_work_without_a_handoff_closes_nothing():
    """Встречный вопрос до передачи второй слой не открывает и не закрывает."""
    messages = [message(1, text="Закройте неиспользуемые сеансы"),
                message(2, COMPANY, seconds=300, text="Подскажите, удалось войти?")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, QUESTION, 1)},
                            staff={2: IRINA})
    assert items[1].handoff_at is None
    assert items[1].substantive_at is None


async def test_a_link_past_every_open_work_still_closes_nothing():
    """Правило не ослаблено: ссылка мимо открытых работ второй слой не трогает."""
    messages = [message(1, text="Закройте неиспользуемые сеансы"),
                message(2, COMPANY, seconds=300, text="Передала ваш запрос"),
                message(3, COMPANY, seconds=600, text="Подскажите, удалось войти?")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1),
                                       3: (None, False, QUESTION, 999)},
                            staff={2: IRINA, 3: TAMARA})
    assert items[1].substantive_at is None


# ── Ссылки нет: прежний путь и его границы ───────────────────────────────

async def test_an_unlinked_question_of_the_handing_manager_keeps_the_wait_open():
    """Вопрос того же менеджера, что передал, без ссылки — не закрывает."""
    messages = [message(1, text="Закройте неиспользуемые сеансы"),
                message(2, COMPANY, seconds=300, text="Передала ваш запрос"),
                message(3, COMPANY, seconds=600, text="Подскажите, удалось войти?")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1),
                                       3: (None, False, QUESTION, None)},
                            staff={2: IRINA, 3: IRINA})
    assert items[1].handoff_at == messages[1].sent_at
    assert items[1].substantive_at is None


async def test_an_unlinked_question_of_another_staff_member_still_closes_the_only_wait():
    """Как прежде: встречный вопрос ВЫШЕДШЕГО специалиста ожидание выполняет."""
    messages = [message(1, text="Закройте неиспользуемые сеансы"),
                message(2, COMPANY, seconds=300, text="Передала ваш запрос"),
                message(3, COMPANY, seconds=600, text="Подскажите, удалось войти?")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1),
                                       3: (None, False, QUESTION, None)},
                            staff={2: IRINA, 3: TAMARA})
    assert items[1].substantive_message_id == 3


async def test_two_specialist_waits_without_a_link_close_neither():
    """При двух ожиданиях без ссылки движок не выбирает."""
    messages = [message(1, text="Закройте неиспользуемые сеансы"),
                message(2, COMPANY, seconds=60, text="Передала ваш запрос"),
                message(3, text="И пришлите акт сверки"),
                message(4, COMPANY, seconds=120, text="Передала бухгалтеру"),
                message(5, COMPANY, seconds=300, text="Подскажите, удалось войти?")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1), 3: REQUEST,
                                       4: (None, False, HANDOFF, 3),
                                       5: (None, False, QUESTION, None)},
                            staff={2: IRINA, 4: IRINA, 5: TAMARA})
    assert items[1].substantive_at is items[3].substantive_at is None


# ── Границы: чего встречный вопрос НЕ меняет ─────────────────────────────

async def test_an_unlinked_handoff_with_a_question_still_starts_no_wait():
    """Без ссылки передача с вопросом срока специалиста не вешает вовсе."""
    messages = [message(1, text="Закройте неиспользуемые сеансы"),
                message(2, text="И акт сверки пришлите"),
                message(3, COMPANY, seconds=300, text="Передала ваш запрос, удалось войти?")]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST,
                                       3: (None, False, HANDOFF, None)},
                            staff={3: IRINA})
    assert items[1].handoff_at is items[2].handoff_at is None
    assert items[1].substantive_at is items[2].substantive_at is None


async def test_a_linked_ack_without_a_question_still_keeps_the_wait_open():
    """Сторож признака: «принято» передавшего менеджера ожидание не снимает."""
    messages = [message(1, text="Закройте неиспользуемые сеансы"),
                message(2, COMPANY, seconds=300, text="Передала ваш запрос"),
                message(3, COMPANY, seconds=600, text="Держим на контроле")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1),
                                       3: (None, False, ACK, 1)},
                            staff={2: IRINA, 3: IRINA})
    assert items[1].substantive_at is None


@pytest.mark.parametrize("version", [2, 3])
async def test_v1_to_v3_are_untouched_by_the_counter_question_rule(version):
    """Правило живёт под `v4_mode`: в старых ветках встречный вопрос ожидание не закрывает."""
    messages = [message(1, text="Закройте неиспользуемые сеансы"),
                message(2, COMPANY, seconds=304,
                        text="Передала ваш запрос, подскажите удалось войти?")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1)},
                            staff={2: IRINA}, version=version)
    assert items[1].substantive_at is None
