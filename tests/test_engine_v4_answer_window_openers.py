"""Окно ожидания ответа клиента в правилах v4: что его вскрывает и что остаётся ответом.

После вопроса компании реплика клиента обычно считается ответом. Обращение со своим
сроком всё же открывают: прямая просьба связаться с меткой `offline`, поручение рядом
с ответом (вежливое повелительное, повелительное кроме «давайте», «прошу/просьба»,
«нужно + глагол») и, при метке `question`, вопросительное слово без «?». В правилах
v2/v3 окно гаснет первой репликой клиента. Метки и ссылки — заданный вход.
"""

from datetime import timedelta

import pytest

from app.services import episodes
from tests.test_answer_window import QUESTION, replay
from tests.test_classify_context_v11 import COMPANY, REQUEST, message

OFFLINE = (False, None, "offline", None)
ACK = (False, None, "ack", None)
CLIENT_QUESTION = (True, None, "question", None)


# ── 1. Просьба связаться / перезвонить с меткой `offline` ────────────────


async def test_offline_contact_request_opens_work():
    """«Свяжитесь со мной, пожалуйста» с меткой `offline` — обращение и срок."""
    messages = [
        message(5731, seconds=0, text="Добрый день! Ирина, свяжитесь со мной, пожалуйста"),
        message(5732, COMPANY, seconds=30 * 60 + 20, text="Добрый день, наберу вас"),
    ]
    items, result = await replay(
        messages, {5731: OFFLINE, 5732: (None, True, "substantive", None)},
        after=timedelta(hours=2),
    )
    assert 5731 in result["response_required_ids"]
    assert items[5731].first_reaction_at == messages[1].sent_at
    assert items[5731].ttfr_seconds == 30 * 60 + 20
    assert items[5731].sla_breached is True


async def test_offline_call_request_inside_the_window_opens_work():
    """«Перезвоните» сразу после вопроса компании и фото клиента."""
    messages = [
        message(5744, COMPANY, seconds=0,
                text="Добрый день. Пытаюсь до вас дозвониться, не получается. "
                     "Скажите, когда вам удобно поговорить?"),
        message(5745, seconds=66, media="photo"),
        message(5746, seconds=77, text="Перезвоните"),
    ]
    items, result = await replay(
        messages, {5744: QUESTION, 5746: OFFLINE}, after=timedelta(hours=2),
    )
    assert 5746 in result["response_required_ids"]
    assert 5746 in items


@pytest.mark.parametrize("text", [
    "Час позвонят",                      # рассказ, не поручение
    "Звонил вам",                        # то же
    "Свяжусь с банком завтра и напишу",  # клиент свяжется сам
    "Мы со своей стороны тоже уже связались с банком",
    "Добрый день! Сегодня будет удобно связаться около 15 часов",
])
async def test_client_telling_about_calls_opens_nothing(text):
    """Изъявительные формы того же корня обращения не открывают."""
    messages = [message(10, seconds=0, text=text)]
    _, result = await replay(messages, {10: OFFLINE}, after=timedelta(hours=2))
    assert 10 not in result["response_required_ids"]


async def test_cannot_reach_the_bank_stays_silent():
    """«Не могу дозвониться» до банка — жалоба, а не просьба.

    Стебля «звони» в маркерах просьбы о звонке нет.
    """
    messages = [
        message(4530, COMPANY, seconds=0, text="Пришлите, пожалуйста, справки из банков"),
        message(4534, seconds=12 * 60,
                text="Добрый день, отправил на почту два банка. третий банк — "
                     "пока справки не пришло, ещё один банк пока не вижу и не могу "
                     "дозвониться, хотя в статусе справка готова."),
    ]
    _, result = await replay(messages, {4530: QUESTION, 4534: REQUEST},
                             after=timedelta(hours=2))
    assert 4534 not in result["response_required_ids"]


# ── 2. Новая просьба рядом с ответом клиента ─────────────────────────────


def _case_instruction_next_to_an_answer():
    return [
        message(6439, COMPANY, seconds=0,
                text="Добрый день. во вложении таблица со сведениями по авансам за "
                     "июль 2026 года. Просьба дать обратную связь, по каким платежам "
                     "готовим закрывающие документы, а какие остаются авансами."),
        message(6449, seconds=1066, text="Добрый день!  Все платежи закрываем"),
        message(6450, seconds=1098,
                text="Посмотрите, пожалуйста, какие ещё счета закрыты полностью и "
                     "по ним тоже УПД сделайте пожалуйста"),
    ]


async def test_new_instruction_next_to_an_answer_opens_work():
    """Поручение рядом с ответом получает свой срок."""
    messages = _case_instruction_next_to_an_answer()
    items, result = await replay(messages, {6439: QUESTION, 6449: REQUEST, 6450: REQUEST},
                                 after=timedelta(hours=2))
    assert 6450 in result["response_required_ids"]
    assert 6450 in items
    # Первая реплика остаётся ответом: её движок не трогает.
    assert 6449 not in result["response_required_ids"]


async def test_polite_agreement_inside_the_window_opens_nothing():
    """«Да, пожалуйста» — согласие, а не поручение: вежливости мало."""
    messages = [message(1, COMPANY, seconds=0, text="Выставить счёт на сентябрь?"),
                message(2, seconds=600, text="Да, пожалуйста")]
    _, result = await replay(messages, {1: QUESTION, 2: REQUEST}, after=timedelta(hours=2))
    assert 2 not in result["response_required_ids"]


CONSENT_WITH_DAVAITE = [
    ("Ясно, но сентябрь еще не рассчитан, кто за него оплатит?",
     "Ну давайте не будем )  мы же продолжаем с вами работать, просто у нас поменялось юрлицо.  "
     "Разумеется, ваши услуги за сентябрь я оплачу."),
    ("Куда вам удобнее прислать документы — в чат или в Битрикс?", "давайте в битрикс"),
    ("Можем подготовить сверку за квартал, сделать?", "Да, давайте сделаем"),
]


@pytest.mark.parametrize("company,client", CONSENT_WITH_DAVAITE)
async def test_davaite_inside_the_window_is_consent_not_an_instruction(company, client):
    """«Давайте» в ответ на вопрос компании — согласие на её предложение, а не поручение."""
    messages = [message(1, COMPANY, seconds=0, text=company),
                message(2, seconds=600, text=client)]
    _, result = await replay(messages, {1: QUESTION, 2: REQUEST}, after=timedelta(hours=2))
    assert 2 not in result["response_required_ids"]


@pytest.mark.parametrize("client", [
    "Самую маленькую пп загрузите в банк, пож-та",   # повелительное рядом не «давайте»
    "Встречу нужно назначить на среду",               # «нужно + глагол»
    "Давайте, пожалуйста, пришлите акт сверки",         # «давайте» + настоящее поручение
])
async def test_a_real_instruction_inside_the_window_still_opens_a_work(client):
    messages = [message(1, COMPANY, seconds=0, text="Какой пакет банковского обслуживания выбираем?"),
                message(2, seconds=600, text=client)]
    items, result = await replay(messages, {1: QUESTION, 2: REQUEST}, after=timedelta(hours=2))
    assert 2 in result["response_required_ids"]
    assert 2 in items


def test_davaite_is_an_imperative_but_not_an_instruction():
    """«Давайте» не поручение; вежливая форма с ним и сам список повелительных — как есть.

    Список `_imperatives` читают и распознаватели подтверждения оплаты и просьбы
    только позвонить, поэтому «давайте» из него не убирается.
    """
    assert not episodes.instruction("Да, давайте сделаем")
    assert episodes.polite_instruction("Давайте, пожалуйста")
    assert episodes._imperatives("Да, давайте сделаем") == ["давайте"]


async def test_second_answer_inside_the_window_stays_silent():
    """«Да.» → «Узнаю у какого банка проще .» — обе реплики ответ.

    Вторая реплика клиента внутри окна остаётся ответом, если маркера
    поручения и вопросительного слова в ней нет.
    """
    messages = [
        message(2855, COMPANY, seconds=0, text="Вы же напишете по итогу?"),
        message(2858, seconds=120, text="Да."),
        message(2860, seconds=480, text="Узнаю у какого банка проще ."),
    ]
    _, result = await replay(messages, {2855: QUESTION, 2858: ACK,
                                        2860: CLIENT_QUESTION}, after=timedelta(hours=2))
    assert 2860 not in result["response_required_ids"]


# ── 3. Вопрос клиента без знака вопроса ──────────────────────────────────


def _case_question_without_a_question_mark():
    return [
        message(6243, COMPANY, seconds=0,
                text="Добрый день. Напоминаем, что за вашим ИП есть задолженность "
                     "в размере 27 800 руб., за обслуживание в августе. Нам важно "
                     "понимать, на какую дату рассчитывать по оплате, чтобы "
                     "сспланировать работу. Пожалуйста, дайте обратную связь, "
                     "по вопросу оплаты. Спасибо за понимание."),
        message(6404, seconds=12537,
                text="А вы распишите пожалуйста подробнее откуда такая сумма."),
        message(6407, seconds=12596,
                text="Стоимость обслуживания растёт каждый месяц. Хочется понимать"),
    ]


async def test_question_without_a_question_mark_opens_work():
    """«…Почему такая сумма.» — вопрос, хотя знака вопроса нет."""
    messages = _case_question_without_a_question_mark()
    items, result = await replay(messages, {6243: QUESTION, 6404: CLIENT_QUESTION,
                                            6407: CLIENT_QUESTION},
                                 after=timedelta(hours=4))
    assert 6404 in result["response_required_ids"]
    assert 6404 in items


async def test_wants_to_understand_is_a_question_too():
    """«Хочется понимать» — тот же вопрос, разбитый на две реплики."""
    messages = _case_question_without_a_question_mark()
    _, result = await replay(messages, {6243: QUESTION, 6404: ACK,
                                        6407: CLIENT_QUESTION}, after=timedelta(hours=4))
    assert 6407 in result["response_required_ids"]


@pytest.mark.parametrize("text,label", [
    ("Завтра внесу после обеда", "request"),
    ("уточняю", "request"),
    ("В обработке еще пишут", "question"),
    ("Пока не могу отправить, чуть позже", "request"),
])
async def test_answers_inside_the_window_stay_silent(text, label):
    """Ответ без поручения, «?» и вопросительного слова обращения не открывает."""
    messages = [message(1, COMPANY, seconds=0, text="Когда внесёте оплату за август?"),
                message(2, seconds=600, text=text)]
    _, result = await replay(messages, {1: QUESTION, 2: (True, None, label, None)},
                             after=timedelta(hours=2))
    assert 2 not in result["response_required_ids"]


# ── 4. Правила v2/v3 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("version", [2, 3])
async def test_older_rule_versions_close_the_window_with_the_first_reply(version):
    """В v2/v3 окно гаснет первой репликой клиента: следующая просьба — обычное обращение.

    Разбор текста внутри окна живёт только в правилах v4 и ветку v2/v3 не меняет.
    """
    messages = _case_instruction_next_to_an_answer()
    _, result = await replay(messages, {6439: QUESTION, 6449: REQUEST, 6450: REQUEST},
                             version=version, after=timedelta(hours=2))
    assert 6449 not in result["response_required_ids"]
    assert 6450 in result["response_required_ids"]
