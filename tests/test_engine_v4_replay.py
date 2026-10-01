"""Причинный движок v4 на повторе без БД и модели; метки и ссылки — заданный вход.

Первый слой: реплика компании — общая реакция на все ожидающие просьбы (адресной
её делает только Telegram-reply), метка `other` тоже реакция, но не ответ. Второй слой
закрывается ссылкой или, без неё, только при единственном ожидании специалиста.
"""

from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType

import pytest

from app.db.models import InteractionState
from app.services.ai import company_payload, validate_verdict
from app.services.episodes import rebuild_interactions
from tests.test_classify_context_v11 import COMPANY, REQUEST, T0, batch, message

HANDOFF = "handoff"
ACK = "ack"


async def replay(messages, verdicts, *, staff=None, version=4):
    snapshot = batch(messages, verdicts).replay_input
    snapshot = replace(
        snapshot, attribution_map=MappingProxyType(staff or {}),
        rules_since=tuple(T0 - timedelta(days=1) if version >= v else None for v in (2, 3, 4)),
    )
    result = await rebuild_interactions(
        None, replay_input=snapshot, persist=False,
        now=T0 + timedelta(minutes=10), settle_open=False,
    )
    return {item.opened_by_message_id: item for item in result["items"]}, result


# ── Первый слой: общая и адресная реакция ────────────────────────────────

@pytest.mark.parametrize("text", ["Принято", "Спасибо, принято", "принято, подготовим", "Хорошо"])
async def test_unlinked_company_reply_is_a_general_first_reaction_to_every_open_request(text):
    """Общее подтверждение снимает первую реакцию со ВСЕХ ожидающих; текст роли не играет."""
    first, second = message(1, text="Нужен акт"), message(2, text="Помогите с законом")
    ack = message(3, COMPANY, text=text)
    items, _ = await replay([first, second, ack], {1: REQUEST, 2: REQUEST, 3: (None, False, ACK, None)})
    assert items[1].first_reaction_message_id == items[2].first_reaction_message_id == 3


@pytest.mark.parametrize("label,substantive", [(ACK, False), ("substantive", True), ("promise", False), ("question", False)])
async def test_company_file_without_text_or_link_reacts_to_every_open_request(label, substantive):
    """Файла от компании достаточно для первой реакции, содержимое не проверяем."""
    first, second = message(1, text="Нужны закрывающие"), message(2, text="Помогите с законом")
    sent = message(3, COMPANY, media="document")
    verdicts = {1: REQUEST, 2: REQUEST}
    if label is not None:
        verdicts[3] = (None, substantive, label, None)
    items, _ = await replay([first, second, sent], verdicts)
    assert items[1].first_reaction_message_id == items[2].first_reaction_message_id == 3


async def test_unclassified_company_file_still_reacts_to_every_open_request():
    """Голый файл в модель не попадает вовсе — и всё равно это реакция."""
    first, second = message(1, text="Нужны закрывающие"), message(2, text="Помогите с законом")
    items, _ = await replay([first, second, message(3, COMPANY, media="document")], {1: REQUEST, 2: REQUEST})
    assert items[1].first_reaction_message_id == items[2].first_reaction_message_id == 3


@pytest.mark.parametrize("text,media", [
    ("Принято, акт", None), ("Акт отправила на почту", None), ("Принято", "document"),
])
async def test_company_reply_with_a_link_but_without_reply_credits_every_request(text, media):
    """Номер от модели первую реакцию не сужает — сужает только Telegram-reply."""
    first, second = message(1, text="Нужен акт"), message(2, text="Помогите с законом")
    reply = message(3, COMPANY, text=text, media=media)
    items, _ = await replay([first, second, reply], {1: REQUEST, 2: REQUEST, 3: (None, True, "substantive", 2)})
    assert items[1].first_reaction_message_id == items[2].first_reaction_message_id == 3


async def test_other_label_reacts_to_every_open_request_but_closes_nothing():
    """«С понедельника я в отпуске» — `other`: молчание в чате нарушено, это первая реакция
    всем открытым обращениям без реакции, но не ответ и не закрытие."""
    first, second = message(1, text="Нужен акт"), message(2, text="Помогите с законом")
    aside = message(3, COMPANY, text="С понедельника я в отпуске")
    items, _ = await replay([first, second, aside], {1: REQUEST, 2: REQUEST, 3: (None, False, "other", None)})
    assert items[1].first_reaction_message_id == items[2].first_reaction_message_id == 3
    assert items[1].substantive_at is items[2].substantive_at is None
    assert items[1].state is items[2].state is InteractionState.REACTED


async def test_other_label_with_a_stray_link_reacts_but_closes_nothing():
    request = message(1, text="Нужен акт")
    aside = message(2, COMPANY, text="Ушла на обед")
    items, _ = await replay([request, aside], {1: REQUEST, 2: (None, True, "other", 1)})
    assert items[1].first_reaction_message_id == 2
    assert items[1].substantive_at is None
    assert items[1].state is InteractionState.REACTED
    items, _ = await replay(
        [request, aside, message(3, COMPANY, text="Акт отправила")],
        {1: REQUEST, 2: (None, True, "other", 1), 3: (None, True, "substantive", 1)},
    )
    assert items[1].first_reaction_message_id == 2
    assert items[1].substantive_message_id == 3


async def test_greeting_alone_is_still_not_a_first_reaction():
    """Одного приветствия недостаточно, даже когда любая другая реплика — реакция."""
    first = message(1, text="Нужен акт")
    items, _ = await replay(
        [first, message(2, COMPANY, text="Добрый день!"), message(3, COMPANY, text="Принято")],
        {1: REQUEST, 2: (None, False, ACK, None), 3: (None, False, ACK, None)},
    )
    assert items[1].first_reaction_message_id == 3


async def test_general_reaction_never_rewrites_an_existing_first_reaction():
    """Устаревшая работа не подхватывает позднее общее «принято»."""
    first = message(1, text="Нужен акт")
    early = message(2, COMPANY, seconds=120, text="Принято")
    late = message(3, COMPANY, seconds=480, text="Принято в работу")
    items, _ = await replay([first, early, late], {1: REQUEST, 2: (None, False, ACK, None), 3: (None, False, ACK, None)})
    assert items[1].first_reaction_message_id == 2
    assert items[1].ttfr_seconds == 119


async def test_unlinked_handoff_reacts_to_everyone_but_starts_no_specialist_wait():
    """«Передала бухгалтеру» без темы — реакция всем, но срок специалиста не вешаем."""
    messages = [message(1, text="Нужен акт"), message(2, text="Нужна декларация"),
                message(3, COMPANY, text="Передала ваш вопрос главному бухгалтеру")]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST, 3: (None, False, HANDOFF, None)}, staff={3: 10})
    assert items[1].first_reaction_message_id == items[2].first_reaction_message_id == 3
    assert items[1].handoff_at is items[2].handoff_at is None


# ── Второй слой ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("author,label,substantive,closes", [
    (20, ACK, False, True),        # установленный другой сотрудник принял
    (10, ACK, False, False),       # общий ack передавшего менеджера — не ответ
    # Результат передавшего менеджера засчитывается ТОЛЬКО со ссылкой:
    # без неё «Сделали» не называет тему, и ожидание сохраняется.
    (10, "substantive", True, False),
    (20, "substantive", True, True),   # результат другого сотрудника
    (None, ACK, False, True),      # неизвестный автор после передачи — компания вышла на связь
    (20, HANDOFF, False, False),   # передача не закрывает передачу
])
async def test_single_specialist_wait_without_link_follows_the_author_and_label(author, label, substantive, closes):
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, COMPANY, text="Вижу, принял в работу")]
    staff = {2: 10, **({3: author} if author is not None else {})}
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1),
                                       3: (None, substantive, label, None)}, staff=staff)
    assert items[1].handoff_at == messages[1].sent_at
    assert (items[1].substantive_message_id == 3) is closes


async def test_two_specialist_waits_without_link_close_neither():
    """Выбирать между двумя ожиданиями движок не вправе."""
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, text="Нужна декларация"), message(4, COMPANY, text="Передала специалисту"),
                message(5, COMPANY, text="Сделали")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1), 3: REQUEST,
                                       4: (None, False, HANDOFF, 3), 5: (None, True, "substantive", None)},
                            staff={2: 10, 4: 10, 5: 20})
    assert items[1].substantive_at is items[3].substantive_at is None


async def test_manager_explicit_result_can_close_specialist_without_personal_reply():
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, COMPANY, text="Бухгалтер подписала акт, отправляю")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1),
                                      3: (None, True, "substantive", 1)}, staff={2: 10, 3: 10})
    assert items[1].substantive_message_id == 3


async def test_known_ack_author_counts_as_specialist_when_handoff_author_unknown():
    """Автор передачи не определён — установленный сотрудник после неё считается другим, его
    «принято» — выход специалиста на связь, как в v1–v3."""
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, COMPANY, text="Принято")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, 1),
                                       3: (None, False, ACK, 1)}, staff={3: 20})
    assert items[1].handoff_at == messages[1].sent_at
    assert items[1].handoff_staff_id is None
    assert items[1].substantive_message_id == 3


async def test_link_resolves_through_every_message_of_the_work():
    """Модель связала ответ с повтором вопроса 4991, а не с открывателем: ссылка ищется по всей работе."""
    messages = [message(4949, text="200 тыс + 6% это будет 212 тыс?"),
                message(4965, COMPANY, text="Передала ваш вопрос главному бухгалтеру"),
                message(4991, text="А по % я так и не поняла"),
                message(5030, COMPANY, text="Если я правильно Вас поняла...")]
    items, _ = await replay(messages, {
        4949: (True, None, "question", None), 4965: (None, False, HANDOFF, 4949),
        4991: (False, None, "addition", None), 5030: (None, True, "substantive", 4991),
    }, staff={4965: 10, 5030: 20})
    assert set(items) == {4949}
    assert items[4949].handoff_at == messages[1].sent_at
    assert items[4949].substantive_message_id == 5030


async def test_specialist_reply_closes_the_only_remaining_wait():
    """Повтор вопроса стал своей работой, ссылка ушла на неё.

    Её закрытие оставляет ровно одно ожидание специалиста, и следующий ответ
    без ссылки от другого сотрудника (не того, кто передавал) закрывает исходный вопрос.
    """
    messages = [message(4949, text="200 тыс + 6% это будет 212 тыс?"),
                message(4965, COMPANY, text="Передала ваш вопрос главному бухгалтеру"),
                message(4991, text="А по % я так и не поняла"),
                message(5030, COMPANY, text="Если я правильно Вас поняла, нужна оплата ИП"),
                message(5034, COMPANY, text="200 000 — это остаток после вычета 6%")]
    items, _ = await replay(messages, {
        4949: (True, None, "question", None), 4965: (None, False, HANDOFF, 4949),
        4991: (True, None, "question", None),
        5030: (None, True, "substantive", 4991), 5034: (None, True, "substantive", None),
    }, staff={4965: 10, 5030: 20, 5034: 20})
    assert items[4991].first_reaction_message_id == 5030
    assert items[4949].substantive_message_id == 5034


@pytest.mark.parametrize("link", [None, 1976])
async def test_handoff_of_one_of_two_requests(link):
    """«Передала запрос бухгалтеру» — реакция обеим просьбам, даже со ссылкой на одну."""
    messages = [message(1970, text="Нужна сверка по налогам"),
                message(1976, text="И когда будет отчёт?"),
                message(2012, COMPANY, text="Доброе утро, передала запрос бухгалтеру")]
    items, _ = await replay(messages, {1970: REQUEST, 1976: (True, None, "question", None),
                                       2012: (None, False, HANDOFF, link)}, staff={2012: 10})
    assert {mid for mid, item in items.items()
            if item.first_reaction_message_id == 2012} == {1970, 1976}


@pytest.mark.parametrize("link", [None, 3538])
async def test_reminder_and_original_request_share_a_general_answer(link):
    """Напоминание получает своё обращение; ответ закрывает первую реакцию обоим."""
    messages = [message(3361, text="Оплатите, пожалуйста, эти счета"),
                message(3538, seconds=7200, text="Подскажите, что со счетами?"),
                message(3555, COMPANY, seconds=7500, text="Пп в банке")]
    items, _ = await replay(messages, {3361: REQUEST, 3538: (True, None, "question", None),
                                       3555: (None, True, "substantive", link)})
    assert {mid for mid, item in items.items()
            if item.first_reaction_message_id == 3555} == {3361, 3538}


async def test_rent_instruction_keeps_its_own_work_and_is_reacted_in_chat():
    """Аренда — своя работа, но первую реакцию ей даёт уже 1601.

    Любая реплика сотрудника в чате снимает первый срок со всех открытых работ.
    """
    messages = [message(1580, text="Подготовьте зарплату за август"),
                message(1592, text="Давайте в Сбер. В точку надо аренду арендодателю"),
                message(1601, COMPANY, text="Зарплатные платежки в банке"),
                message(1623, COMPANY, text="ПП по аренде в банке точка")]
    items, _ = await replay(messages, {
        1580: REQUEST, 1592: (True, None, "mixed_request", None),
        1601: (None, True, "substantive", 1580), 1623: (None, True, "substantive", 1592),
    })
    assert set(items) == {1580, 1592}
    assert items[1580].first_reaction_message_id == 1601
    assert items[1592].first_reaction_message_id == 1601


# ── Клиентская сторона: воронка, ответы и ложные сроки ───────────────────

async def test_new_requests_during_a_specialist_wait_open_their_own_works():
    """Переданная работа не глотает новые просьбы."""
    messages = [message(2354, seconds=0, text="Нужна копия декларации 2025"),
                message(2360, COMPANY, seconds=60, text="Передала бухгалтеру"),
                message(3302, seconds=6000, text="Сформируйте, пожалуйста, три счёта"),
                message(3315, seconds=6600, text="Каждый счет на 3 месяца"),
                message(4783, seconds=12000, text="Если платеж не поступал, можно ли делать закрывающие?")]
    items, result = await replay(messages, {
        2354: REQUEST, 2360: (None, False, HANDOFF, 2354), 3302: REQUEST,
        3315: (False, None, "correction", None), 4783: (True, None, "question", None),
    }, staff={2360: 10})
    assert set(items) == {2354, 3302, 4783}
    assert {3302, 4783}.issubset(result["response_required_ids"])
    members = {item.opened_by_message_id: mids for item, mids in result["members"]}
    assert members[2354] == [2354]
    assert 3315 in members[3302]


async def test_client_clarification_of_its_own_pending_question_starts_no_deadline():
    """Короткое пояснение к ещё не отвеченному вопросу — addition, а не новый срок."""
    messages = [message(526, seconds=0, text="Почему выплата такая маленькая?"),
                message(528, seconds=120, text="Изменение ставки должно увеличить выплату в 5 раз")]
    items, result = await replay(messages, {526: (True, None, "question", None),
                                            528: (False, None, "addition", None)})
    assert set(items) == {526}
    assert 528 not in result["response_required_ids"]


async def test_file_with_its_single_processing_instruction_is_one_work():
    """Файл и единственная инструкция по нему — одна работа."""
    messages = [message(1462, text="Договор подписан", media="document"),
                message(1463, seconds=1476, text="По эдо допик 2")]
    items, result = await replay(messages, {1462: REQUEST, 1463: (False, None, "addition", None)})
    assert set(items) == {1462}
    assert items[1462].opened_at == messages[0].sent_at
    assert 1463 not in result["response_required_ids"]
    members = {item.opened_by_message_id: mids for item, mids in result["members"]}
    assert members[1462] == [1462, 1463]


def test_mixed_task_label_overrides_missing_or_false_model_flag():
    assert validate_verdict({"label": "mixed_request", "requires_response": False}, is_client=True) == {
        "label": "mixed_request", "requires_response": True,
    }


def test_other_is_a_valid_company_label_and_never_substantive():
    assert validate_verdict({"label": "other", "answers_request_id": 5}, is_client=False,
                            open_request_ids=[5]) == {
        "label": "other", "is_substantive": False, "answers_request_id": 5,
    }


@pytest.mark.parametrize("label,requires,expected", [
    ("mixed_request", True, True), ("answer", False, False),
    ("request", True, False), ("question", True, False),
])
async def test_only_mixed_request_survives_the_answer_window(label, requires, expected):
    """Внутри окна ожидания ответа клиента новое дело открывает только `mixed_request`.

    `request`/`question` в окне считаются ответом.
    """
    question = message(1, COMPANY, text="Зарплату за август?")
    client = message(2, text="Да, за июль. и оплатите аренду" if expected else "Да, за август")
    items, result = await replay([question, client], {1: (None, False, "question", None), 2: (requires, None, label, None)})
    assert (2 in result["response_required_ids"]) is expected
    if expected:
        assert items[2].opened_at == client.sent_at


async def test_mixed_task_breaks_the_answer_attachment_series():
    """Вопрос компании без просьбы прислать материал: после `mixed_request` файл — уже не часть ответа."""
    messages = [message(1, COMPANY, text="Договор уже подписали?"), message(2, text="Да, вот он"),
                message(3, text="И оплатите аренду"), message(4, media="document")]
    _, result = await replay(messages, {1: (None, False, "question", None), 2: (False, None, "answer", None),
                                       3: (True, None, "mixed_request", None), 4: REQUEST})
    assert 2 not in result["response_required_ids"]
    assert {3, 4}.issubset(result["response_required_ids"])


async def test_requested_material_after_a_mixed_task_is_still_the_answer():
    """«Пришлите договор» взводит ожидание материала: файл без текста в его пределах — ответ,
    даже после новой просьбы клиента."""
    messages = [message(1, COMPANY, text="Пришлите договор"), message(2, text="Вот он"),
                message(3, text="И оплатите аренду"), message(4, media="document")]
    _, result = await replay(messages, {1: (None, False, "question", None), 2: (False, None, "answer", None),
                                       3: (True, None, "mixed_request", None), 4: REQUEST})
    assert 3 in result["response_required_ids"]
    assert not ({2, 4} & set(result["response_required_ids"]))


async def test_requested_attachment_series_does_not_start_timer():
    messages = [message(1, COMPANY, text="Пришлите договор"), message(2, media="document"), message(3, media="document")]
    _, result = await replay(messages, {1: (None, False, "question", None)})
    assert not ({2, 3} & set(result["response_required_ids"]))


async def test_captioned_requested_file_with_answer_label_keeps_attachment_series():
    messages = [message(1, COMPANY, text="Пришлите договор"),
                message(2, text="Вот подписанный договор", media="document"), message(3, media="document")]
    _, result = await replay(messages, {1: (None, False, "question", None), 2: (False, None, "answer", None)})
    assert not ({2, 3} & set(result["response_required_ids"]))


@pytest.mark.parametrize("version,requires", [(1, True), (2, False), (3, False), (4, False)])
async def test_wrong_request_label_on_pure_answer_costs_a_false_signal_only_in_v1(version, requires):
    """В v4, как в v2/v3, ошибочный `request` на чистом ответе молчит.

    В v1 окно ожидания жило только внутри открытого обращения, поэтому там
    сигнал остаётся.
    """
    messages = [message(1, COMPANY, text="Зарплату за август?"), message(2, text="Да, за август")]
    _, result = await replay(messages, {1: (None, False, "question", None), 2: REQUEST}, version=version)
    assert (2 in result["response_required_ids"]) is requires


async def test_a_request_inside_the_answer_window_opens_a_work_only_by_its_text_or_label():
    """Внутри окна ожидания ответа клиента метка `request` — ответ, если текст не поручение.

    Своё дело открывают метка `mixed_request` и поручение («Выставьте, пожалуйста, счёт»).
    После ПЕРЕДАЧИ просьба своё обращение получает и так — см.
    `test_new_requests_during_a_specialist_wait_open_their_own_works`.
    """
    question = message(3284, COMPANY, seconds=0, text="Когда вам удобно созвониться?")
    answer = message(3302, seconds=60, text="Завтра после обеда, в 15:00")
    _, result = await replay([question, answer], {3284: (None, False, "question", None), 3302: REQUEST})
    assert 3302 not in result["response_required_ids"]

    items, result = await replay([question, answer], {3284: (None, False, "question", None),
                                                      3302: (True, None, "mixed_request", None)})
    assert 3302 in result["response_required_ids"]
    assert items[3302].opened_at == answer.sent_at

    invoice = message(3302, seconds=60, text="Выставьте, пожалуйста, счет на оплату")
    items, result = await replay([question, invoice], {3284: (None, False, "question", None), 3302: REQUEST})
    assert 3302 in result["response_required_ids"]
    assert items[3302].opened_at == invoice.sent_at


async def test_bare_unsolicited_photo_during_old_specialist_wait_survives():
    messages = [message(863, seconds=0, text="Подготовьте акт"),
                message(864, COMPANY, seconds=1, text="Передала бухгалтеру"),
                message(2192, seconds=60, media="photo"),
                message(2194, COMPANY, seconds=90, text="Этот документ передала бухгалтеру")]
    items, result = await replay(messages, {863: REQUEST, 864: (None, False, HANDOFF, 863),
                                          2194: (None, False, HANDOFF, 2192)}, staff={864: 10, 2194: 10})
    assert set(items) == {863, 2192}
    assert 2192 in result["response_required_ids"]
    assert items[863].substantive_at is None
    assert items[863].handoff_at == messages[1].sent_at
    assert items[2192].first_reaction_message_id == 2194
    assert items[2192].handoff_at == messages[3].sent_at


@pytest.mark.parametrize("label", ["addition", "correction"])
@pytest.mark.parametrize("boundary", ["before_ack", "after_ack", "after_gap"])
async def test_multiple_files_of_client_update_batch_end_at_own_ack_or_gap(label, boundary):
    messages = [message(1, seconds=0, text="Подготовьте акт"),
                message(2, COMPANY, seconds=1, text="Передала бухгалтеру"),
                message(3, seconds=60, text="Поправка реквизитов"),
                message(4, seconds=90, media="document"), message(5, seconds=120, media="photo")]
    verdicts = {1: REQUEST, 2: (None, False, HANDOFF, 1), 3: (False, None, label, None)}
    if boundary == "after_ack":
        messages.append(message(6, COMPANY, seconds=150, text="Принято"))
        verdicts[6] = (None, False, ACK, 1)
    last_seconds = 721 if boundary == "after_gap" else 180
    messages.append(message(7, seconds=last_seconds, media="document"))
    items, result = await replay(messages, verdicts, staff={2: 10, 6: 10})
    members = {item.opened_by_message_id: ids for item, ids in result["members"]}
    assert {3, 4, 5}.issubset(members[1])
    if boundary == "before_ack":
        assert set(items) == {1}
        assert 7 in members[1]
    else:
        assert set(items) == {1, 7}
        assert 7 not in members[1]
        assert items[7].first_reaction_at is None
    assert items[1].handoff_at == messages[1].sent_at


async def test_attachment_metadata_reaches_real_open_items_without_hidden_content():
    attachment = message(1, media="document")
    target = message(2, COMPANY, text="Принято")
    context = await batch([attachment], {1: REQUEST}).context(target)
    assert context[1][0].has_media and context[1][0].media_kind == "document"
    payload = company_payload(target.text, *context)
    assert "[документ]" in payload.split("ОТКРЫТЫЕ ОБРАЩЕНИЯ")[1]


async def test_greeting_with_wrong_model_link_cannot_credit_or_remove_pending_work():
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Добрый день!")]
    verdicts = {1: REQUEST, 2: (None, True, "substantive", 1)}
    items, _ = await replay(messages, verdicts)
    assert items[1].first_reaction_at is items[1].substantive_at is None
    target = message(3, COMPANY, text="Акт отправила")
    context = await batch(messages, verdicts).context(target)
    assert [item.message_id for item in context[1]] == [1]


# ── История v1–v3 не меняется ────────────────────────────────────────────

async def test_ack_widening_does_not_rewrite_v3_history():
    messages = [message(1, text="Нужен акт"), message(2, text="Помогите с законом"),
                message(3, COMPANY, text="Принято")]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST, 3: (None, False, ACK, 2)}, version=3)
    assert set(items) == {1}
    assert items[1].client_messages == 2


@pytest.mark.parametrize("version", [1, 2, 3])
async def test_strict_unknown_route_keeps_legacy_unlinked_closure_before_v4(version):
    messages = [message(1, text="Нужен акт"), message(2, COMPANY, text="Передала бухгалтеру"),
                message(3, COMPANY, text="Сделали")]
    items, _ = await replay(messages, {1: REQUEST, 2: (None, False, HANDOFF, None),
                                      3: (None, True, "substantive", None)}, staff={2: 10, 3: 20}, version=version)
    assert items[1].handoff_at == messages[1].sent_at
    assert items[1].substantive_message_id == 3
