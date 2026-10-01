"""Первая реакция адресуется транспортом для любой метки сотрудника: без
Telegram-reply — всем открытым ожиданиям чата, с reply — только той работе.
Второй слой остаётся строгим; правила «своей работы» и дублей соседних просьб не задеты."""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import TransportActorKind
from app.services.alerts import duplicate_burst_ids
from tests.test_classify_context_v11 import COMPANY, REQUEST, message
from tests.test_engine_v4_replay import ACK, HANDOFF, replay

SUBSTANTIVE, PROMISE, QUESTION = "substantive", "promise", "question"
OTHER, CORRECTION = "other", "correction"
# Все метки сотрудника, которые дают первую реакцию всем открытым работам.
REACTING_LABELS = [ACK, SUBSTANTIVE, HANDOFF, PROMISE, QUESTION]
# Два человека со стороны клиента в одном чате (правило «своей работы»).
ANNA, BORIS = 501, 502


def verdict(label, *, link=None):
    """Вердикт сотрудника: `is_substantive` у `substantive` — true, у прочих — false."""
    return (None, label == SUBSTANTIVE, label, link)


def reacted_by(items, message_id):
    return {mid for mid, item in items.items() if item.first_reaction_message_id == message_id}


# ── Первую реакцию адресует транспорт, а не ссылка модели ───────────

@pytest.mark.parametrize("label", REACTING_LABELS)
async def test_any_staff_label_without_reply_reacts_to_every_open_request(label):
    """Без reply любая метка сотрудника — реакция всем открытым работам."""
    messages = [message(1, text="Нужен акт"), message(2, text="Нужна декларация"),
                message(3, COMPANY, text="Передала бухгалтеру")]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST, 3: verdict(label, link=1)},
                            staff={3: 10})
    assert reacted_by(items, 3) == {1, 2}


@pytest.mark.parametrize("label", REACTING_LABELS)
async def test_any_staff_label_with_a_telegram_reply_credits_only_that_work(label):
    """Адресность, видимая в транспорте, сильнее веера — тоже для любой метки."""
    messages = [message(1, text="Нужен акт"), message(2, text="Нужна декларация"),
                message(3, COMPANY, text="Готово", reply_to=2)]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST, 3: verdict(label, link=1)},
                            staff={3: 10})
    assert reacted_by(items, 3) == {2}
    assert items[1].first_reaction_at is None


async def test_reply_to_any_message_of_a_work_credits_that_whole_work():
    """Reply на досланный файл — та же работа, что и его просьба."""
    messages = [message(1, text="Нужен акт"), message(2, text="Нужна декларация"),
                message(3, text="Вот реквизиты"),
                message(4, COMPANY, text="Готово", reply_to=3)]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST, 3: (False, None, "addition", None),
                                       4: verdict(SUBSTANTIVE)}, staff={4: 10})
    assert reacted_by(items, 4) == {2}
    assert items[1].first_reaction_at is None


async def test_reply_outside_every_open_work_reacts_to_all_of_them():
    """Reply мимо открытых работ (на закрытую работу или своё сообщение) читается как реплика
    без reply: молчание нарушено для всех открытых работ."""
    messages = [message(1, text="Нужен акт"), message(2, text="Нужна декларация"),
                message(3, COMPANY, text="Готово", reply_to=99)]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST, 3: verdict(SUBSTANTIVE)},
                            staff={3: 10})
    assert reacted_by(items, 3) == {1, 2}


async def test_link_to_a_closed_work_still_reacts_to_everyone_but_opens_no_handoff():
    """Передача ссылается на уже закрытую работу.

    Первый слой: реплика в чате всё равно нарушила молчание — реакция обеим
    открытым работам. Второй слой: срок специалиста ни на одну из них
    не вешается, тема не названа (для второго слоя ссылка мимо открытых работ по-прежнему не считается).
    """
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Акт отправила"),
                message(3, text="Нужна декларация"), message(4, text="И справка"),
                message(5, COMPANY, text="Передала ваш вопрос главному бухгалтеру")]
    items, _ = await replay(messages, {1: REQUEST, 2: verdict(SUBSTANTIVE, link=1), 3: REQUEST,
                                       4: REQUEST, 5: verdict(HANDOFF, link=1)},
                            staff={2: 10, 5: 10})
    assert reacted_by(items, 5) == {3, 4}
    assert items[3].handoff_at is items[4].handoff_at is None


async def test_other_label_reacts_to_every_open_work_whatever_its_link():
    """`other` («с понедельника я в отпуске») — первая реакция всем открытым работам без
    реакции, ссылка модели её не сужает; ответом по существу реплика не становится."""
    messages = [message(1, text="Нужен акт"), message(2, text="Нужна декларация"),
                message(3, COMPANY, text="С понедельника я в отпуске")]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST, 3: verdict(OTHER, link=1)},
                            staff={3: 10})
    assert reacted_by(items, 3) == {1, 2}
    assert items[1].substantive_at is items[2].substantive_at is None


async def test_greeting_alone_still_reacts_to_nobody():
    """Приветствие не реакция, даже когда любая другая реплика — веер."""
    messages = [message(1, text="Нужен акт"), message(2, text="Нужна декларация"),
                message(3, COMPANY, text="Добрый день!"),
                message(4, COMPANY, text="Передала бухгалтеру")]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST, 3: verdict(SUBSTANTIVE),
                                       4: verdict(HANDOFF, link=1)}, staff={3: 10, 4: 10})
    assert reacted_by(items, 3) == set()
    assert reacted_by(items, 4) == {1, 2}


async def test_integrator_notice_still_reacts_to_nobody():
    """«Вы не авторизованы» — служебный ответ бота, не реплика сотрудника."""
    messages = [message(1, text="Нужен акт"), message(2, text="Нужна декларация"),
                message(3, COMPANY, text="Вы не авторизованы",
                        actor=TransportActorKind.INTEGRATOR_BOT)]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST})
    assert items[1].first_reaction_at is items[2].first_reaction_at is None


async def test_company_file_without_text_still_reaches_every_request():
    """Голый файл компании без reply — общая реакция."""
    messages = [message(1, text="Нужны закрывающие"), message(2, text="Нужна декларация"),
                message(3, COMPANY, media="document")]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST})
    assert reacted_by(items, 3) == {1, 2}


async def test_general_reaction_still_never_rewrites_an_existing_first_reaction():
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, seconds=120, text="Принято"),
                message(3, seconds=240, text="Нужна декларация"),
                message(4, COMPANY, seconds=480, text="Уточню и вернусь")]
    items, _ = await replay(messages, {1: REQUEST, 2: verdict(ACK), 3: REQUEST,
                                       4: verdict(PROMISE, link=3)}, staff={2: 10, 4: 10})
    assert items[1].first_reaction_message_id == 2
    assert items[3].first_reaction_message_id == 4


# ── Второй слой остаётся строгим ─────────────────────────────────────────

async def test_handoff_with_a_link_opens_the_second_layer_only_for_the_named_work():
    """Первый слой — обеим, второй — только указанной (R-19, R-21)."""
    messages = [message(1, text="Нужен акт"), message(2, text="Нужна декларация"),
                message(3, COMPANY, text="Ваш вопрос по декларации передала бухгалтеру")]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST, 3: verdict(HANDOFF, link=2)},
                            staff={3: 10})
    assert reacted_by(items, 3) == {1, 2}
    assert items[2].handoff_at == messages[2].sent_at
    assert items[1].handoff_at is None


async def test_handoff_without_a_link_opens_no_second_layer_at_all():
    """Срок специалиста нельзя повесить на угаданную тему."""
    messages = [message(1, text="Нужен акт"), message(2, text="Нужна декларация"),
                message(3, COMPANY, text="Передала ваш вопрос главному бухгалтеру")]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST, 3: verdict(HANDOFF)},
                            staff={3: 10})
    assert reacted_by(items, 3) == {1, 2}
    assert items[1].handoff_at is items[2].handoff_at is None


async def test_substantive_without_a_link_does_not_close_the_second_layer():
    """Второй слой без ссылки: «Менеджер: Сделали» без ссылки ожидание не снимает."""
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, COMPANY, text="Сделали")]
    items, _ = await replay(messages, {1: REQUEST, 2: verdict(HANDOFF, link=1),
                                       3: verdict(SUBSTANTIVE)}, staff={2: 10, 3: 10})
    assert items[1].handoff_at == messages[1].sent_at
    assert items[1].substantive_at is None


async def test_specialist_contact_without_a_link_still_closes_the_only_wait():
    """Установленный ДРУГОЙ сотрудник вышел на связь по единственному ожиданию."""
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, COMPANY, text="Вижу, принял в работу")]
    items, _ = await replay(messages, {1: REQUEST, 2: verdict(HANDOFF, link=1), 3: verdict(ACK)},
                            staff={2: 10, 3: 20})
    assert items[1].substantive_message_id == 3


async def test_manager_result_with_a_link_closes_the_wait():
    """Результат передавшего менеджера засчитывается со ссылкой."""
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, COMPANY, text="Бухгалтер подписала акт")]
    items, _ = await replay(messages, {1: REQUEST, 2: verdict(HANDOFF, link=1),
                                       3: verdict(SUBSTANTIVE, link=1)}, staff={2: 10, 3: 10})
    assert items[1].substantive_message_id == 3


async def test_two_specialist_waits_without_a_link_close_neither():
    """Выбирать между двумя ожиданиями движок не вправе."""
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, text="Нужна декларация"), message(4, COMPANY, text="Передала специалисту"),
                message(5, COMPANY, text="Сделали")]
    items, _ = await replay(messages, {1: REQUEST, 2: verdict(HANDOFF, link=1), 3: REQUEST,
                                       4: verdict(HANDOFF, link=3), 5: verdict(SUBSTANTIVE)},
                            staff={2: 10, 4: 10, 5: 20})
    assert items[1].substantive_at is items[3].substantive_at is None


async def test_link_to_a_closed_work_does_not_close_the_only_specialist_wait():
    """Ссылка мимо открытых работ на втором слое: «мимо» значит «адресовано не этим работам»."""
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Акт отправила"),
                message(3, text="Нужна декларация"), message(4, COMPANY, text="Передала бухгалтеру"),
                message(5, COMPANY, text="Готово по акту")]
    items, _ = await replay(messages, {1: REQUEST, 2: verdict(SUBSTANTIVE, link=1), 3: REQUEST,
                                       4: verdict(HANDOFF, link=3), 5: verdict(SUBSTANTIVE, link=1)},
                            staff={2: 10, 4: 10, 5: 20})
    assert items[3].first_reaction_message_id == 4
    assert items[3].substantive_at is None


# ── Своя работа и дубли соседних просьб не задеты ──────────────────────────────────────────────

async def test_correction_from_another_client_person_still_opens_its_own_request():
    messages = [message(1, text="Подготовьте договор", author=ANNA),
                message(2, COMPANY, text="Принято в работу"),
                message(3, text="Просьба поправить место проведения", author=BORIS)]
    items, result = await replay(messages, {1: REQUEST, 2: verdict(ACK, link=1),
                                            3: (False, None, CORRECTION, None)})
    assert set(items) == {1, 3}
    assert 3 in result["response_required_ids"]
    assert items[3].first_reaction_at is None


async def test_addition_from_the_same_client_person_still_stays_in_the_work():
    messages = [message(1, text="Подготовьте договор", author=ANNA),
                message(2, COMPANY, text="Принято в работу"),
                message(3, text="Реквизиты во вложении", author=ANNA)]
    items, result = await replay(messages, {1: REQUEST, 2: verdict(ACK, link=1),
                                            3: (False, None, "addition", None)})
    assert set(items) == {1}
    assert 3 not in result["response_required_ids"]


def test_duplicate_burst_collapse_still_lives_in_the_alert_layer():
    from types import SimpleNamespace

    start = datetime(2026, 9, 14, 8, 27, tzinfo=timezone.utc)
    burst = [
        SimpleNamespace(id=10, chat_id=438, thread_id=None, opened_at=start),
        SimpleNamespace(id=11, chat_id=438, thread_id=None, opened_at=start + timedelta(minutes=1)),
        SimpleNamespace(id=12, chat_id=900, thread_id=None, opened_at=start + timedelta(minutes=1)),
    ]
    assert duplicate_burst_ids(burst, 30) == {11}


# ── Реакция со ссылкой на соседнюю реплику пачки ─────────────────────────
# Реакция сотрудника приходит вовремя, но со ссылкой на соседнюю реплику
# той же пачки; просрочки нет ни у одной работы пачки.

async def test_handoff_to_one_reply_of_a_burst_leaves_no_signal():
    """«Передала запрос бухгалтеру» со ссылкой на среднюю реплику пачки."""
    messages = [message(1970, seconds=0, text="Нужна сверка по налогам"),
                message(1976, seconds=30, text="И когда будет отчёт?"),
                message(1982, seconds=60, text="Только ваша фирма штрафы присылает"),
                message(2012, COMPANY, seconds=180, text="Доброе утро, передала запрос бухгалтеру")]
    items, _ = await replay(messages, {1970: REQUEST, 1976: (True, None, QUESTION, None),
                                       1982: REQUEST, 2012: verdict(HANDOFF, link=1976)},
                            staff={2012: 10})
    assert reacted_by(items, 2012) == {1970, 1976, 1982}
    assert not any(item.sla_breached for item in items.values())


async def test_answer_to_the_first_of_three_replies_leaves_no_signal():
    """Ответ через 2 минуты со ссылкой на первую из трёх реплик."""
    messages = [message(1903, seconds=0, text="Посмотреть где?"),
                message(1904, seconds=10, text="В личном кабинете? В карточках товаров?"),
                message(1905, seconds=20, text="Не совсем понимаем"),
                message(1906, COMPANY, seconds=120, text="В карточке товара, вкладка «Остатки»")]
    items, _ = await replay(messages, {1903: (True, None, QUESTION, None),
                                       1904: (True, None, QUESTION, None),
                                       1905: (True, None, QUESTION, None),
                                       1906: verdict(QUESTION, link=1903)}, staff={1906: 10})
    assert reacted_by(items, 1906) == {1903, 1904, 1905}
    assert not any(item.sla_breached for item in items.values())


async def test_substantive_to_the_neighbour_leaves_no_signal():
    """Ответ по существу через 8 минут со ссылкой на соседнюю реплику."""
    messages = [message(1200, seconds=0, text="Сделайте сверку по контрагенту"),
                message(1204, seconds=60, text="И подскажите по НДС"),
                message(1205, COMPANY, seconds=480, text="По НДС ставка 20%, сверку готовим")]
    items, _ = await replay(messages, {1200: REQUEST, 1204: (True, None, QUESTION, None),
                                       1205: verdict(SUBSTANTIVE, link=1204)}, staff={1205: 10})
    assert reacted_by(items, 1205) == {1200, 1204}
    assert not any(item.sla_breached for item in items.values())


async def test_handoff_to_the_older_question_leaves_no_signal_for_the_burst():
    """«Передала бухгалтеру» через 4 минуты со ссылкой на более ранний вопрос."""
    messages = [message(4949, seconds=0, text="200 тыс + 6% это будет 212 тыс?"),
                message(4957, seconds=60, text="Это что получается, я плачу налог?"),
                message(4958, seconds=90, text="Я не понимаю логику, объясните"),
                message(4965, COMPANY, seconds=240, text="Передала бухгалтеру")]
    items, _ = await replay(messages, {4949: (True, None, QUESTION, None), 4957: REQUEST,
                                       4958: REQUEST, 4965: verdict(HANDOFF, link=4949)},
                            staff={4965: 10})
    assert reacted_by(items, 4965) == {4949, 4957, 4958}
    assert not any(item.sla_breached for item in items.values())
    # Второй слой строг: передача открыта только названной работе.
    assert items[4949].handoff_at == messages[3].sent_at
    assert items[4957].handoff_at is items[4958].handoff_at is None


async def test_promise_to_the_neighbour_leaves_no_signal():
    """«Уточню информацию» в ту же минуту со ссылкой на соседнюю реплику."""
    messages = [message(3926, seconds=0, text="Когда будет акт сверки?"),
                message(3931, seconds=30, text="И по остаткам на складе"),
                message(3939, COMPANY, seconds=60, text="Уточню информацию")]
    items, _ = await replay(messages, {3926: (True, None, QUESTION, None), 3931: REQUEST,
                                       3939: verdict(PROMISE, link=3926)}, staff={3939: 10})
    assert reacted_by(items, 3939) == {3926, 3931}
    assert not any(item.sla_breached for item in items.values())
