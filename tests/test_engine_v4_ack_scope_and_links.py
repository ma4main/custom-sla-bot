"""Правки причинного движка v4 без БД и модели.

  * адресность подтверждения — область действия подтверждения (`ack`) движок решает по МЕТАДАННЫМ
    Telegram (явный reply), а не по номеру от модели; второй слой не трогает;
  * своя работа — `addition`/`correction` требуют СВОЕЙ открытой работы: той, куда
    этот же человек со стороны клиента уже писал;
  * ссылка мимо открытых — ссылка на работу, которой среди открытых нет, второй слой
    не открывает и не закрывает;
  * второй слой без ссылки — `substantive` без ссылки второй слой не закрывает;
  * дубли соседних просьб — дубли сигналов соседних просьб движком не закрываются,
    схлопывание живёт в `alerts.duplicate_burst_ids`.

Адресация первой реакции транспортом — в `test_engine_v4_transport_addressing.py`.
Метки и ссылки здесь — заданный вход.
"""

from datetime import timedelta

import pytest

from app.services.alerts import duplicate_burst_ids
from tests.test_classify_context_v11 import COMPANY, REQUEST, message
from tests.test_engine_v4_replay import ACK, HANDOFF, replay

ADDITION, CORRECTION = "addition", "correction"
SUBSTANTIVE = "substantive"
# Два человека со стороны клиента в одном чате.
ANNA, BORIS = 501, 502


def members_of(result):
    return {item.opened_by_message_id: mids for item, mids in result["members"]}


# ── Область действия подтверждения ───────────────────────────────────────

async def test_ack_without_reply_credits_every_open_request_even_with_a_link():
    """Номер от модели область действия `ack` больше не сужает."""
    first, second = message(1, text="Нужен акт"), message(2, text="Нужна декларация")
    ack = message(3, COMPANY, text="Принято в работу")
    items, _ = await replay([first, second, ack], {1: REQUEST, 2: REQUEST, 3: (None, False, ACK, 1)})
    assert items[1].first_reaction_message_id == items[2].first_reaction_message_id == 3


async def test_ack_without_reply_and_without_link_still_credits_everyone():
    first, second = message(1, text="Нужен акт"), message(2, text="Нужна декларация")
    items, _ = await replay([first, second, message(3, COMPANY, text="Принято")],
                            {1: REQUEST, 2: REQUEST, 3: (None, False, ACK, None)})
    assert items[1].first_reaction_message_id == items[2].first_reaction_message_id == 3


async def test_ack_with_a_telegram_reply_credits_only_the_replied_request():
    """Адресность, видимая в транспорте, сильнее веера."""
    first, second = message(1, text="Нужен акт"), message(2, text="Нужна декларация")
    ack = message(3, COMPANY, text="Принято", reply_to=2)
    items, _ = await replay([first, second, ack], {1: REQUEST, 2: REQUEST, 3: (None, False, ACK, 1)})
    assert items[2].first_reaction_message_id == 3
    assert items[1].first_reaction_at is None


async def test_ack_reply_to_any_message_of_a_work_credits_that_work():
    """Reply на досланный файл — та же работа, что и его просьба."""
    messages = [message(1, text="Нужен акт"), message(2, text="Нужна декларация"),
                message(3, text="Вот реквизиты"), message(4, COMPANY, text="Принято", reply_to=3)]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST, 3: (False, None, ADDITION, None),
                                       4: (None, False, ACK, 1)})
    assert items[2].first_reaction_message_id == 4
    assert items[1].first_reaction_at is None


async def test_ack_replying_outside_every_open_work_reacts_to_all_of_them():
    """Reply мимо открытых работ (на закрытую работу или своё сообщение) читается как реплика
    без reply — реакция всем открытым."""
    first, second = message(1, text="Нужен акт"), message(2, text="Нужна декларация")
    ack = message(3, COMPANY, text="Принято", reply_to=99)
    items, _ = await replay([first, second, ack], {1: REQUEST, 2: REQUEST, 3: (None, False, ACK, None)})
    assert items[1].first_reaction_message_id == items[2].first_reaction_message_id == 3


async def test_ack_widening_neither_opens_nor_closes_the_second_layer():
    """Адресность подтверждения трогает только первый слой: передача осталась ждать специалиста."""
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, text="Нужна декларация"), message(4, COMPANY, text="Принято")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1), 3: REQUEST,
                                       4: (None, False, ACK, 3)}, staff={2: 10, 4: 10})
    assert items[1].first_reaction_message_id == 2
    assert items[3].first_reaction_message_id == 4
    assert items[1].handoff_at == messages[1].sent_at
    assert items[1].substantive_at is None


async def test_company_file_without_text_or_reply_still_reaches_every_request():
    """Голый файл без reply — общая реакция."""
    first, second = message(1, text="Нужны закрывающие"), message(2, text="Нужна декларация")
    items, _ = await replay([first, second, message(3, COMPANY, media="document")],
                            {1: REQUEST, 2: REQUEST})
    assert items[1].first_reaction_message_id == items[2].first_reaction_message_id == 3


@pytest.mark.parametrize("label", [SUBSTANTIVE, HANDOFF, "promise", "question"])
async def test_non_ack_labels_now_fan_out_like_ack(label):
    """Адресность распространена с `ack` на любую метку сотрудника.

    Своевременная реакция с номером соседней реплики той же пачки даёт
    первую реакцию обеим работам.
    """
    first, second = message(1, text="Нужен акт"), message(2, text="Нужна декларация")
    answer = message(3, COMPANY, text="Готово по акту")
    items, _ = await replay([first, second, answer],
                            {1: REQUEST, 2: REQUEST, 3: (None, label == SUBSTANTIVE, label, 1)},
                            staff={3: 10})
    assert items[1].first_reaction_message_id == items[2].first_reaction_message_id == 3


# ── Поправка и дополнение требуют СВОЕЙ работы ──────────────────────

async def test_correction_from_another_client_person_opens_its_own_request():
    messages = [message(1, text="Подготовьте договор", author=ANNA),
                message(2, COMPANY, text="Принято в работу"),
                message(3, text="Просьба поправить место проведения", author=BORIS)]
    items, result = await replay(messages, {1: REQUEST, 2: (None, False, ACK, 1),
                                            3: (False, None, CORRECTION, None)})
    assert set(items) == {1, 3}
    assert 3 in result["response_required_ids"]
    assert items[3].opened_at == messages[2].sent_at
    assert items[3].first_reaction_at is None


async def test_addition_from_another_client_person_also_opens_its_own_request():
    messages = [message(1, text="Подготовьте договор", author=ANNA),
                message(2, COMPANY, text="Принято в работу"),
                message(3, text="И счёт на второй интенсив", author=BORIS)]
    items, result = await replay(messages, {1: REQUEST, 2: (None, False, ACK, 1),
                                            3: (False, None, ADDITION, None)})
    assert set(items) == {1, 3}
    assert 3 in result["response_required_ids"]


async def test_addition_from_the_same_client_person_stays_in_the_work():
    messages = [message(1, text="Подготовьте договор", author=ANNA),
                message(2, COMPANY, text="Принято в работу"),
                message(3, text="Реквизиты во вложении", author=ANNA)]
    items, result = await replay(messages, {1: REQUEST, 2: (None, False, ACK, 1),
                                            3: (False, None, ADDITION, None)})
    assert set(items) == {1}
    assert members_of(result)[1] == [1, 3]
    assert 3 not in result["response_required_ids"]


async def test_addition_joins_the_work_its_author_already_wrote_into():
    """«Своя» работа — та, куда человек уже писал, а не только открыл."""
    messages = [message(1, text="Подготовьте договор", author=ANNA),
                message(2, media="document", author=BORIS),
                message(3, COMPANY, text="Принято"),
                message(4, text="Реквизиты в файле выше", author=BORIS)]
    items, result = await replay(messages, {1: REQUEST, 3: (None, False, ACK, None),
                                            4: (False, None, ADDITION, None)})
    assert set(items) == {1}
    assert members_of(result)[1] == [1, 2, 4]


async def test_correction_before_first_reaction_still_shifts_its_own_deadline():
    messages = [message(1, text="Подготовьте договор", author=ANNA),
                message(2, text="Не 10 копий, а 12", author=ANNA)]
    items, _ = await replay(messages, {1: REQUEST, 2: (False, None, CORRECTION, None)})
    assert set(items) == {1}
    assert items[1].opened_at == messages[1].sent_at


async def test_unknown_client_author_keeps_the_addition_inside_the_work():
    """Подписи нет — работа считается своей, дубля нет."""
    messages = [message(1462, text="[файл]"), message(1463, seconds=1476, text="По эдо допик 2")]
    items, result = await replay(messages, {1462: REQUEST, 1463: (False, None, ADDITION, None)})
    assert set(items) == {1462}
    assert members_of(result)[1462] == [1462, 1463]
    assert 1463 not in result["response_required_ids"]


# ── Ссылка мимо открытых работ ───────────────────────────────────────────

async def test_link_to_a_closed_work_opens_no_handoff():
    """Ссылка мимо открытых работ осталась правилом ВТОРОГО слоя.

    Реплика сотрудника в чате молчание всё равно нарушает, поэтому первую
    реакцию получают обе открытые работы; срок специалиста не вешается
    ни на одну — тема не названа.
    """
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Акт отправила"),
                message(3, text="Нужна декларация"), message(4, text="И справка"),
                message(5, COMPANY, text="Передала ваш вопрос главному бухгалтеру")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, True, SUBSTANTIVE, 1), 3: REQUEST,
                                       4: REQUEST, 5: (None, False, HANDOFF, 1)}, staff={2: 10, 5: 10})
    assert items[3].first_reaction_message_id == items[4].first_reaction_message_id == 5
    assert items[3].handoff_at is items[4].handoff_at is None


async def test_link_to_a_closed_work_does_not_close_the_only_specialist_wait():
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Акт отправила"),
                message(3, text="Нужна декларация"), message(4, COMPANY, text="Передала бухгалтеру"),
                message(5, COMPANY, text="Готово по акту")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, True, SUBSTANTIVE, 1), 3: REQUEST,
                                       4: (None, False, HANDOFF, 3), 5: (None, True, SUBSTANTIVE, 1)},
                            staff={2: 10, 4: 10, 5: 20})
    assert items[3].substantive_at is None


# ── Второй слой без ссылки ───────────────────────────────────────────────

async def test_manager_result_without_link_keeps_the_specialist_wait_open():
    """«Менеджер: Сделали» без адресации ожидание не снимает."""
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, COMPANY, text="Сделали")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1),
                                       3: (None, True, SUBSTANTIVE, None)}, staff={2: 10, 3: 10})
    assert items[1].substantive_at is None


async def test_specialist_ack_without_link_still_closes_the_only_wait():
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, COMPANY, text="Вижу, принял в работу")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1),
                                       3: (None, False, ACK, None)}, staff={2: 10, 3: 20})
    assert items[1].substantive_message_id == 3


async def test_manager_result_with_a_link_closes_the_wait():
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, COMPANY, text="Бухгалтер подписала акт")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1),
                                       3: (None, True, SUBSTANTIVE, 1)}, staff={2: 10, 3: 10})
    assert items[1].substantive_message_id == 3


# ── Дубли сигналов соседних просьб ───────────────────────────────────────

async def test_two_adjacent_requests_still_get_their_own_works_and_deadlines():
    """Движком дубли НЕ схлопываются: у каждой просьбы свой срок."""
    messages = [message(1, text="Это что получается, я плачу налог?"),
                message(2, text="Я не понимаю логику, объясните")]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST})
    assert set(items) == {1, 2}


def test_duplicate_burst_ids_keeps_the_first_signal_of_one_client_burst():
    """Схлопывание живёт в слое оповещений: остаётся самая ранняя просьба."""
    from types import SimpleNamespace
    from datetime import datetime, timezone

    start = datetime(2026, 9, 14, 8, 27, tzinfo=timezone.utc)
    burst = [
        SimpleNamespace(id=10, chat_id=438, thread_id=None, opened_at=start),
        SimpleNamespace(id=11, chat_id=438, thread_id=None, opened_at=start + timedelta(minutes=1)),
        SimpleNamespace(id=12, chat_id=438, thread_id=None, opened_at=start + timedelta(minutes=1)),
        # За окном первой реакции — самостоятельная просрочка.
        SimpleNamespace(id=13, chat_id=438, thread_id=None, opened_at=start + timedelta(minutes=45)),
        # Другой чат не схлопывается с этим.
        SimpleNamespace(id=14, chat_id=900, thread_id=None, opened_at=start + timedelta(minutes=1)),
    ]
    assert duplicate_burst_ids(burst, 30) == {11, 12}
