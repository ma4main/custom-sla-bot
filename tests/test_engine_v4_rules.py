"""Правила движка версии 4: окно ожидания, отзыв, дополнения и второй слой.

Файл проверяет итоговое поведение `rebuild_interactions` в `v4_mode`:

    окно ожидания ответа клиента и «пустая» реплика компании;
    просьба позвонить/связаться при метке `offline`;
    продолжение своей речи (`addition`/`correction`);
    отзыв просьбы клиентом;
    дополнение ищет свою работу среди всех открытых;
    Telegram-reply мимо открытых работ;
    неатрибутированный автор на втором слое;
    пустой `is_substantive`;
    граница версий правил.

Метки, ссылки и авторство здесь — заданный вход, а не предсказание модели.
Хелперы и эталонные сценарии файла используют и соседние тесты движка.
"""

from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType

import pytest

from app.db.models import InteractionState
from app.services import episodes
from app.services.episodes import rebuild_interactions
from tests.test_classify_context_v11 import COMPANY, REQUEST, T0, batch, message

# Вердикт: (requires_response, is_substantive, label, answers_request_id).
CLIENT_QUESTION = (True, None, "question", None)
CLIENT_ACK = (False, None, "ack", None)
CLIENT_ADDITION = (False, None, "addition", None)
CLIENT_CORRECTION = (False, None, "correction", None)
CLIENT_OFFLINE = (False, None, "offline", None)
CLIENT_SOCIAL = (False, None, "social", None)
COMPANY_QUESTION = (None, False, "question", None)
COMPANY_SUBSTANTIVE = (None, True, "substantive", None)
COMPANY_PROMISE = (None, False, "promise", None)
COMPANY_ACK = (None, False, "ack", None)
COMPANY_OTHER = (None, False, "other", None)

IRINA = "Ирина Соколова [corp.example.com] пишет:\n\n"
DARYA = "Дарья Лебедева [corp.example.com] пишет:\n\n"
KSENIA = "Ксения Морозова [corp.example.com] пишет:\n\n"
POLINA = "Полина Орлова [corp.example.com] пишет:\n\n"

STAFF_IRINA, STAFF_KSENIA, STAFF_DARYA = 11, 22, 33

# Два сотрудника со стороны клиента в одном чате.
CLIENT_A, CLIENT_B = 7000000001, 7000000002


def msk(hour, minute=0, second=0, day=0):
    """Секунды от T0 (понедельник, 12:00 МСК) по часам МСК; день 10:00–19:00."""
    return day * 86400 + (hour - 12) * 3600 + minute * 60 + second


async def replay(messages, verdicts, *, staff=None, after=timedelta(hours=6),
                 rules=None, settle_open=False):
    """Прогнать чат через движок без БД и вернуть обращения по открывающему сообщению.

    `settle_open=False` по умолчанию: финальный проход закрыл бы обращения
    по своим правилам и спрятал то, что проверяется, — состояние на момент
    реплики. `rules` — кортеж смещений в секундах от `T0` для границ v2/v3/v4
    (нужен только тестам границы версий); по умолчанию весь чат под v4.
    """
    snapshot = batch(messages, verdicts).replay_input
    snapshot = replace(
        snapshot, attribution_map=MappingProxyType(staff or {}),
        rules_since=(
            (T0 - timedelta(days=1),) * 3 if rules is None
            else tuple(None if value is None else T0 + timedelta(seconds=value)
                       for value in rules)
        ),
    )
    result = await rebuild_interactions(
        None, replay_input=snapshot, persist=False, now=T0 + after,
        settle_open=settle_open,
    )
    return {item.opened_by_message_id: item for item in result["items"]}, result


def members(result):
    """Состав каждого обращения по открывающему сообщению."""
    return {item.opened_by_message_id: list(ids) for item, ids in result["members"]}


# ── «Пустая» реплика компании и окно ожидания ────────────────────────────
#
# Реплика компании с меткой `other` или чистое приветствие окно ожидания
# ответа клиента не гасит и не взводит: движок такие реплики отбрасывает.


def _window_case(filler, filler_verdict):
    """Вопрос компании → «пустая» реплика компании → ответ клиента."""
    messages = [
        message(1, COMPANY, seconds=msk(9, 50),
                text=IRINA + "Уточните, счёт на ИП или на ООО?"),
    ]
    verdicts = {1: COMPANY_QUESTION}
    if filler is not None:
        messages.append(message(2, COMPANY, seconds=msk(10, 0), text=IRINA + filler))
        verdicts[2] = filler_verdict
    messages.append(message(3, seconds=msk(10, 5), author=CLIENT_A,
                            text="Сейчас посмотрю в договоре"))
    verdicts[3] = REQUEST
    return messages, verdicts


@pytest.mark.parametrize("filler,verdict", [
    ("Коллеги, завтра офис не работает", COMPANY_OTHER),
    ("Доброе утро", CLIENT_SOCIAL),
])
async def test_an_inert_company_reply_keeps_the_answer_window(filler, verdict):
    """Реплика, которую движок и так отбрасывает, окна не гасит."""
    messages, verdicts = _window_case(filler, verdict)
    _, result = await replay(messages, verdicts,
                             staff={1: STAFF_IRINA, 2: STAFF_IRINA})
    assert 3 in result["answers"]
    assert 3 not in result["response_required_ids"]


async def test_an_inert_company_question_does_not_arm_the_window():
    """Болтовня с меткой `other`, кончающаяся «?», ход клиенту не отдаёт.

    Иначе настоящая просьба клиента следом объявлялась бы ответом без срока.
    """
    messages = [
        message(1, COMPANY, seconds=msk(10, 0),
                text=IRINA + "Как отпуск прошёл?"),
        message(2, seconds=msk(10, 5), author=CLIENT_A,
                text="Подготовьте акт сверки за август"),
    ]
    items, result = await replay(messages, {1: COMPANY_OTHER, 2: REQUEST},
                                 staff={1: STAFF_IRINA})
    assert 2 not in result["answers"]
    assert 2 in result["response_required_ids"]
    assert items[2].state is InteractionState.OPEN


async def test_a_normal_company_reply_closes_the_window():
    """Обычное «принято» окно гасит: не отбрасывается только `other`/приветствие."""
    messages, verdicts = _window_case("Принято, сейчас уточню", COMPANY_ACK)
    _, result = await replay(messages, verdicts,
                             staff={1: STAFF_IRINA, 2: STAFF_IRINA})
    assert 3 not in result["answers"]


async def test_a_real_company_question_arms_the_window():
    """Вопрос компании отдаёт ход клиенту — его реплика следом ответ."""
    messages, verdicts = _window_case(None, None)
    _, result = await replay(messages, verdicts, staff={1: STAFF_IRINA})
    assert 3 in result["answers"]


# ── Просьба позвонить/связаться при метке `offline` ──────────────────────
#
# Реплика `offline` становится обращением со сроком, только если в ней
# прямая просьба связаться — по форме слова, а не по подстроке.


# Рассказы о звонке: изъявительное наклонение, а не просьба.
CALL_STORIES = [
    "Мне уже позвонили из налоговой, спасибо",
    "Я вам перезвонил, трубку не взяли",
    "Созвонимся завтра",
    "Созвонились вчера, всё выяснили",
    "Нам не могут дозвониться",
    "Звонил вам утром, не дозвонился",
]
CALL_REQUESTS = [
    "Перезвоните, пожалуйста",
    "Ирина, свяжитесь со мной, пожалуйста",
    "Наберите меня, пожалуйста",
    "Жду звонка по ЭТрН",
    "Нужен созвон по отчётности",
    "Давайте созвонимся завтра",
    # Вежливая просьба строится инфинитивом. Обычно у неё метка `request`,
    # но если модель поставит `offline`, просьба обязана остаться просьбой.
    "Здравствуйте, а у Дмитрия номер заканчивается 0000? Не могу дозвониться. "
    "Можете попросить её набрать меня?",
    "Можно с вами созвониться по честному знаку?",
    # Опечатка «-тся» вместо «-ться» тоже ловится.
    "А можете со мной созвонится? вопрос про патент и взносы",
    "Можно с вами связатся после обеда?",
]


@pytest.mark.parametrize("text", CALL_STORIES)
def test_a_story_about_a_call_is_not_a_contact_request(text):
    """Рассказ о звонке просьбой не становится ни в одной форме."""
    assert episodes.contact_request(text) is False


@pytest.mark.parametrize("text", CALL_REQUESTS)
def test_a_real_call_request_is_a_contact_request(text):
    """Настоящие просьбы признак по форме не теряет."""
    assert episodes.contact_request(text) is True


@pytest.mark.parametrize("text", CALL_STORIES)
async def test_an_offline_story_about_a_call_needs_no_response(text):
    """«Мне уже позвонили» с меткой `offline` — ответ уйдёт вне чата, срока нет."""
    messages = [message(1, seconds=msk(10, 0), author=CLIENT_A, text=text)]
    items, result = await replay(messages, {1: CLIENT_OFFLINE},
                                 after=timedelta(hours=6), settle_open=True)
    assert 1 not in result["response_required_ids"]
    assert items[1].state is InteractionState.NO_RESPONSE_NEEDED


@pytest.mark.parametrize("text", CALL_REQUESTS)
async def test_an_offline_call_request_is_a_request_with_a_deadline(text):
    """«Перезвоните, пожалуйста» с меткой `offline` — просьба со сроком."""
    messages = [message(1, seconds=msk(10, 0), author=CLIENT_A, text=text)]
    items, result = await replay(messages, {1: CLIENT_OFFLINE},
                                 after=timedelta(hours=6), settle_open=True)
    assert 1 in result["response_required_ids"]
    assert items[1].state is InteractionState.OPEN


# ── Продолжение своей речи ───────────────────────────────────────────────
#
# `addition`/`correction` без своей открытой работы — продолжение своей
# речи, если своя работа закрылась не больше 10 минут назад и НЕ ответом
# по существу; иначе это новая просьба со сроком.


def _case_late_correction():
    """Поправка клиента после обещания компании в чате с двумя людьми клиента."""
    return [
        message(6972, COMPANY, seconds=0,
                text=DARYA + "Доброе утро. У вас получится войти "
                                "в Диадок, подписать и отправить акты?"),
        message(6973, seconds=39, author=CLIENT_A, text="Добрый день, да"),
        message(6975, seconds=525, author=CLIENT_B, text="Альфа нет ЭЦП"),
        message(6976, COMPANY, seconds=541,
                text=DARYA + "Пожалуйста, зайдите в раздел Документы -> "
                                "черновики, подпишите и отправьте все УПД."),
        message(6977, COMPANY, seconds=542, media="document",
                text=DARYA + "делится файлом"),
        message(6978, COMPANY, seconds=596,
                text=DARYA + "Принято, уточню у коллеги"),
        message(6979, seconds=596, author=CLIENT_A, text="Точно, ЭЦП отозвана"),
        message(7030, COMPANY, seconds=5525,
                text=POLINA + "Добрый день. Тогда так, ставлю себе в план."),
    ]


_LATE_CORRECTION_VERDICTS = {
    6972: COMPANY_QUESTION,
    6973: CLIENT_ACK,
    6975: REQUEST,
    6976: COMPANY_SUBSTANTIVE,
    6977: COMPANY_SUBSTANTIVE,
    6978: COMPANY_PROMISE,
    6979: CLIENT_CORRECTION,
    7030: (None, True, "substantive", 6979),
}

_LATE_CORRECTION_STAFF = {6972: STAFF_DARYA, 6976: STAFF_DARYA,
                          6977: STAFF_DARYA, 6978: STAFF_DARYA,
                          7030: STAFF_KSENIA}


def _case_addition_to_a_silently_closed_work():
    """Дополнение клиента к своей работе, закрывшейся без ответа по существу."""
    messages = [
        message(1, COMPANY, seconds=msk(9, 4),
                text=IRINA + "Получится войти в Диадок сегодня?"),
        message(2, seconds=msk(9, 5), author=CLIENT_A, text="Добрый день, да"),
        message(3, seconds=msk(9, 13), author=CLIENT_A, text="У Альфа нет ЭЦП"),
        message(4, COMPANY, seconds=msk(9, 13, 30),
                text=IRINA + "Тогда нужно перевыпускать, сейчас уточню сроки"),
        message(5, seconds=msk(9, 14, 30), author=CLIENT_A,
                text="Дополню: ЭЦП отозвали ещё в четверг"),
    ]
    verdicts = {1: COMPANY_QUESTION, 2: CLIENT_ACK, 3: REQUEST,
                4: COMPANY_SUBSTANTIVE, 5: CLIENT_ADDITION}
    return messages, verdicts


def _case_correction_after_a_substantive_answer():
    """Поправка ПОСЛЕ ответа по существу — верный сигнал, не шум."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A,
                text="Сделайте акт на 100 000"),
        message(2, COMPANY, seconds=msk(10, 1),
                text=DARYA + "Акт на 100 000 готов, направляю"),
        message(3, seconds=msk(10, 3), author=CLIENT_A, text="Ошибся, сумма 120 000"),
    ]
    verdicts = {1: REQUEST, 2: (None, True, "substantive", 1), 3: CLIENT_CORRECTION}
    return messages, verdicts


async def test_an_addition_to_a_silently_closed_own_work_is_silent():
    """Работа закрылась без ответа по существу — дополнение продолжает речь."""
    messages, verdicts = _case_addition_to_a_silently_closed_work()
    items, result = await replay(messages, verdicts, staff={1: STAFF_IRINA,
                                                            4: STAFF_IRINA},
                                 after=timedelta(seconds=msk(10, 32)))
    assert 5 not in result["response_required_ids"]
    assert items[5].sla_breached is not True


async def test_a_correction_after_a_substantive_answer_opens_a_work():
    """Поправка к ПОЛУЧЕННОМУ результату — новая работа со сроком."""
    messages, verdicts = _case_correction_after_a_substantive_answer()
    items, result = await replay(messages, verdicts, staff={2: STAFF_DARYA},
                                 settle_open=True)
    assert 3 in result["response_required_ids"]
    assert items[3].state is InteractionState.OPEN
    assert items[3].first_reaction_at is None


async def test_a_late_correction_after_a_promise_is_silent():
    """Поправка следом за обещанием компании нового срока не получает."""
    items, result = await replay(_case_late_correction(), _LATE_CORRECTION_VERDICTS,
                                 staff=_LATE_CORRECTION_STAFF)
    assert 6979 not in result["response_required_ids"]
    assert items[6979].sla_breached is not True


async def test_a_correction_by_another_client_person_is_its_own_work():
    """Поправку написал ДРУГОЙ сотрудник клиента — это его собственное дело."""
    messages = [
        message(1550, seconds=0, author=CLIENT_A,
                text="Просьба все договоры и акты отправить сюда на проверку"),
        message(1551, COMPANY, seconds=40, text=IRINA + "принято в работу"),
        message(1552, seconds=181, author=CLIENT_B,
                text="Просьба поправить в договоре место проведения"),
    ]
    items, result = await replay(messages, {1550: REQUEST, 1551: COMPANY_ACK,
                                            1552: CLIENT_CORRECTION},
                                 staff={1551: STAFF_IRINA})
    assert 1552 in result["response_required_ids"]
    assert items[1552].first_reaction_at is None


async def test_the_continuation_window_is_ten_minutes():
    """«Только что» — это десять минут, а не «когда-нибудь раньше»."""
    messages = [
        message(1, seconds=0, author=CLIENT_A, text="Да, всё верно"),
        message(2, COMPANY, seconds=60, text=IRINA + "Принято"),
        message(3, seconds=1860, author=CLIENT_A, text="Точно, ЭЦП отозвана"),
    ]
    _, result = await replay(messages, {1: CLIENT_ACK, 2: COMPANY_ACK,
                                        3: CLIENT_CORRECTION},
                             staff={2: STAFF_IRINA})
    assert 3 in result["response_required_ids"]


async def test_a_real_new_request_after_a_closed_work_stays_a_request():
    """Метка `request` в ветку дополнений не попадает — настоящая просьба цела."""
    messages = [
        message(1, seconds=0, author=CLIENT_A, text="Спасибо, всё получил"),
        message(2, COMPANY, seconds=60, text=IRINA + "Рады помочь"),
        message(3, seconds=120, author=CLIENT_A,
                text="Подготовьте, пожалуйста, акт сверки за август"),
    ]
    items, result = await replay(messages, {1: CLIENT_ACK, 2: COMPANY_ACK,
                                            3: REQUEST},
                                 staff={2: STAFF_IRINA})
    assert 3 in result["response_required_ids"]
    assert items[3].state is InteractionState.OPEN


# ── Отзыв просьбы ────────────────────────────────────────────────────────
#
# При единственном открытом обращении с ожиданием специалиста отзыв
# клиента до срока специалиста снимает ожидание. Сама реплика отзыва —
# последнее слово закрываемой работы, а не новое дело. Метки отзыва —
# белый список; реплика, ставшая просьбой, отзывом не считается.


def _case_withdrawal(label_verdict, *, author=CLIENT_A, seconds=msk(10, 45)):
    """Просьба → передача → отзыв. Метка отзыва — параметр."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A,
                text="Нужна расшифровка по 60 счёту"),
        message(2, COMPANY, seconds=msk(10, 5), text=IRINA + "Передала бухгалтеру"),
        message(3, seconds=seconds, author=author,
                text="Извините, не надо уже, вопрос решился, отбой"),
    ]
    verdicts = {1: REQUEST, 2: (None, False, "handoff", 1)}
    if label_verdict is not None:
        verdicts[3] = label_verdict
    return messages, verdicts


@pytest.mark.parametrize("label_verdict", [
    CLIENT_CORRECTION, CLIENT_ADDITION, None, CLIENT_ACK,
])
async def test_a_withdrawal_stays_inside_the_work_it_closes(label_verdict):
    """Отзыв закрывает работу и входит в её состав; второго обращения нет.

    `None` — строки вердикта у реплики отзыва нет вовсе.
    """
    messages, verdicts = _case_withdrawal(label_verdict)
    items, result = await replay(messages, verdicts, staff={2: STAFF_IRINA},
                                 after=timedelta(seconds=msk(11, 20)))
    assert result["interactions"] == 1
    assert items[1].state is InteractionState.NO_RESPONSE_NEEDED
    assert members(result)[1] == [1, 3]
    # Ответ специалиста не выдуман: ни времени, ни автора.
    assert items[1].substantive_at is None
    assert items[1].substantive_staff_id is None


async def test_a_withdrawal_by_another_client_employee_is_accepted():
    """«Отбой» от коллеги автора принимается — автор не сверяется.

    Открытое обращение в чате ровно одно, и отзыв почти наверняка про него.
    Строгая сверка автора оставляла бы ожидание специалиста висеть —
    ложный алерт второго слоя.
    """
    messages, verdicts = _case_withdrawal(CLIENT_ACK, author=CLIENT_B)
    items, result = await replay(messages, verdicts, staff={2: STAFF_IRINA},
                                 after=timedelta(days=2))
    assert result["interactions"] == 1
    assert items[1].state is InteractionState.NO_RESPONSE_NEEDED
    assert members(result)[1] == [1, 3]


async def test_a_withdrawal_with_a_new_request_is_not_a_withdrawal():
    """«Не надо уже, но пришлите акт» с меткой `request` — новая просьба."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A,
                text="Попросите Дмитрия перезвонить мне"),
        message(2, COMPANY, seconds=msk(10, 5), text=IRINA + "передам ваш запрос"),
        message(3, seconds=msk(10, 45), author=CLIENT_A,
                text="Звонок не надо уже, но пришлите, пожалуйста, акт сверки"),
    ]
    items, result = await replay(messages, {1: REQUEST,
                                            2: (None, False, "handoff", 1),
                                            3: REQUEST},
                                 staff={2: STAFF_IRINA}, after=timedelta(days=2))
    assert items[1].state is InteractionState.REACTED
    assert 3 in result["response_required_ids"]


async def test_a_withdrawal_asking_to_call_back_is_a_request():
    """«Отбой, лучше перезвоните» с меткой `offline` — просьба со своим сроком.

    Метка `offline` в белом списке отзыва есть, но прямая просьба связаться
    уже сделала эту реплику обращением, и глушить её нельзя.
    """
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A,
                text="Нужна расшифровка по 60 счёту"),
        message(2, COMPANY, seconds=msk(10, 5), text=IRINA + "Передала бухгалтеру"),
        message(3, seconds=msk(10, 45), author=CLIENT_A,
                text="Отбой, не надо уже, лучше перезвоните мне"),
    ]
    items, result = await replay(messages, {1: REQUEST,
                                            2: (None, False, "handoff", 1),
                                            3: CLIENT_OFFLINE},
                                 staff={2: STAFF_IRINA}, after=timedelta(days=2))
    assert items[1].state is InteractionState.REACTED
    assert 3 in result["response_required_ids"]


async def test_a_withdrawal_needs_one_open_work_with_a_specialist_wait():
    """Отзыв снимает ожидание, только когда обращение одно и ожидание есть."""
    # Два открытых обращения — неизвестно, что отозвали.
    two = [
        message(1, seconds=msk(10, 0), author=CLIENT_A, text="Нужна расшифровка"),
        message(2, seconds=msk(10, 1), author=CLIENT_A, text="И акт сверки нужен"),
        message(3, COMPANY, seconds=msk(10, 5), text=IRINA + "Передала бухгалтеру"),
        message(4, seconds=msk(10, 45), author=CLIENT_A, text="Извините, отбой"),
    ]
    items, _ = await replay(two, {1: REQUEST, 2: REQUEST,
                                  3: (None, False, "handoff", 1), 4: CLIENT_ACK},
                            staff={3: STAFF_IRINA}, after=timedelta(days=2))
    assert items[1].state is InteractionState.REACTED
    # Ожидания специалиста нет — снимать нечего.
    no_handoff = [
        message(1, seconds=msk(10, 0), author=CLIENT_A, text="Нужна расшифровка"),
        message(2, COMPANY, seconds=msk(10, 5), text=IRINA + "Принято"),
        message(3, seconds=msk(10, 45), author=CLIENT_A, text="Извините, отбой"),
    ]
    items, _ = await replay(no_handoff, {1: REQUEST, 2: COMPANY_ACK, 3: CLIENT_ACK},
                            staff={2: STAFF_IRINA}, after=timedelta(days=2))
    assert items[1].handoff_at is None
    assert items[1].state is InteractionState.REACTED


# ── Дополнение ищет свою работу среди всех открытых ──────────────────────


def _case_addition_by_the_first_author():
    """Дополнение автора ПЕРВОЙ просьбы при открытой чужой."""
    messages = [
        message(1, seconds=msk(12, 0), author=CLIENT_A,
                text="Сделайте счёт на аренду"),
        message(2, seconds=msk(12, 2), author=CLIENT_B,
                text="Нужна справка о задолженности"),
        message(3, seconds=msk(12, 5), author=CLIENT_A,
                text="Счёт на июль и август"),
    ]
    return messages, {1: REQUEST, 2: REQUEST, 3: CLIENT_ADDITION}


async def test_an_addition_joins_its_authors_work_among_several_open():
    """Дополнение ложится в работу СВОЕГО автора, третьей работы нет."""
    messages, verdicts = _case_addition_by_the_first_author()
    items, result = await replay(messages, verdicts)
    assert result["interactions"] == 2
    assert members(result)[1] == [1, 3]
    assert members(result)[2] == [2]
    assert 3 not in result["response_required_ids"]
    assert items[1].client_messages == 2


async def test_a_correction_moves_the_deadline_of_its_own_work():
    """Сдвиг `opened_at` поправкой достаётся СВОЕЙ работе, чужая не тронута."""
    messages = [
        message(1, seconds=msk(12, 0), author=CLIENT_A,
                text="Сделайте платёжку на 100 000"),
        message(2, seconds=msk(12, 1), author=CLIENT_B,
                text="Нужна справка о задолженности"),
        message(3, seconds=msk(12, 5), author=CLIENT_A, text="Поправка: на 120 000"),
    ]
    items, result = await replay(messages, {1: REQUEST, 2: REQUEST,
                                            3: CLIENT_CORRECTION})
    assert result["interactions"] == 2
    assert items[1].opened_at == messages[2].sent_at
    assert items[2].opened_at == messages[1].sent_at


async def test_an_addition_to_the_only_open_work_joins_it():
    """Открытая работа одна — дополнение ложится в неё."""
    messages = [
        message(1, seconds=msk(12, 0), author=CLIENT_A,
                text="Сделайте счёт на аренду"),
        message(3, seconds=msk(12, 5), author=CLIENT_A, text="Счёт на июль и август"),
    ]
    _, result = await replay(messages, {1: REQUEST, 3: CLIENT_ADDITION})
    assert result["interactions"] == 1
    assert members(result)[1] == [1, 3]


async def test_with_two_own_works_the_correction_joins_the_last():
    """При двух СВОИХ работах берётся последняя.

    Движок не знает, к какой из двух собственных просьб относится поправка,
    и не угадывает — как и на втором слое.
    """
    messages = [
        message(1, seconds=msk(12, 0), author=CLIENT_A,
                text="Сделайте счёт на аренду"),
        message(2, seconds=msk(12, 2), author=CLIENT_A,
                text="Нужна справка о задолженности"),
        message(3, seconds=msk(12, 5), author=CLIENT_A,
                text="Сумма 50 000, а не 40 000"),
    ]
    _, result = await replay(messages, {1: REQUEST, 2: REQUEST,
                                        3: CLIENT_CORRECTION})
    assert members(result)[2] == [2, 3]


async def test_an_addition_with_no_own_work_opens_its_own():
    """Своей работы нет вовсе — дополнение становится собственным делом со сроком."""
    messages = [
        message(1, seconds=msk(12, 0), author=CLIENT_B,
                text="Нужна справка о задолженности"),
        message(2, seconds=msk(12, 5), author=CLIENT_A,
                text="Счёт на июль и август"),
    ]
    _, result = await replay(messages, {1: REQUEST, 2: CLIENT_ADDITION})
    assert 2 in result["response_required_ids"]


# ── Telegram-reply мимо открытых работ ───────────────────────────────────
#
# Reply компании, не попавший ни в одну открытую работу, читается как
# реплика без reply — первая реакция всем открытым.


def _case_reply_to_yesterdays_work(reply_to):
    """Вчерашняя работа закрыта, сегодня открыта новая."""
    messages = [
        message(1, seconds=msk(10, 0, day=1), author=CLIENT_A,
                text="Нужна справка о доходах"),
        message(2, COMPANY, seconds=msk(10, 10, day=1),
                text=IRINA + "Справка во вложении"),
        message(3, seconds=msk(9, 0, day=2), author=CLIENT_A,
                text="Ещё нужна справка 2-НДФЛ за 2025"),
        message(4, COMPANY, seconds=msk(9, 15, day=2), reply_to=reply_to,
                text=IRINA + "По вашему вчерашнему запросу — готовлю и 2-НДФЛ тоже"),
    ]
    verdicts = {1: REQUEST, 2: (None, True, "substantive", 1), 3: REQUEST,
                4: COMPANY_PROMISE}
    return messages, verdicts


@pytest.mark.parametrize("reply_to", [1, 2])
async def test_a_reply_past_every_open_work_is_a_reaction_to_everyone(reply_to):
    """Reply адресата среди открытых не называет — значит, это реакция всем."""
    messages, verdicts = _case_reply_to_yesterdays_work(reply_to)
    items, _ = await replay(messages, verdicts, staff={2: STAFF_IRINA,
                                                       4: STAFF_IRINA},
                            after=timedelta(seconds=msk(11, 0, day=2)))
    assert items[3].first_reaction_message_id == 4
    assert items[3].sla_breached is False


async def test_a_reply_into_an_open_work_is_addressed_to_it_alone():
    """Reply, попавший в открытую работу, адресует её одну."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A, text="Нужен акт сверки"),
        message(2, seconds=msk(10, 1), author=CLIENT_B, text="И справка о доходах"),
        message(3, COMPANY, seconds=msk(10, 10), reply_to=1,
                text=IRINA + "Передала бухгалтеру"),
    ]
    items, _ = await replay(messages, {1: REQUEST, 2: REQUEST,
                                       3: (None, False, "handoff", 1)},
                            staff={3: STAFF_IRINA})
    assert items[1].first_reaction_message_id == 3
    assert items[2].first_reaction_at is None


async def test_a_reply_past_open_works_leaves_the_second_layer_alone():
    """Второй слой адресуется ссылкой модели, а не reply: его такой reply не трогает."""
    messages, verdicts = _case_reply_to_yesterdays_work(1)
    items, _ = await replay(messages, verdicts, staff={2: STAFF_IRINA,
                                                       4: STAFF_IRINA},
                            after=timedelta(seconds=msk(11, 0, day=2)))
    assert items[3].handoff_at is None
    assert items[3].substantive_at is None


# ── Неатрибутированный автор на втором слое ──────────────────────────────


def _case_handoff_and_ack_attribution(handoff_staff, reply_staff):
    """Передача и «принял, смотрю» с разной атрибуцией."""
    messages = [
        message(1, seconds=msk(12, 0), author=CLIENT_A,
                text="Нужна расшифровка по 76 счёту"),
        message(2, COMPANY, seconds=msk(12, 3), text=IRINA + "Передала бухгалтеру"),
        message(3, COMPANY, seconds=msk(12, 10), text=KSENIA + "Принял, смотрю"),
    ]
    verdicts = {1: REQUEST, 2: (None, False, "handoff", 1), 3: COMPANY_ACK}
    staff = {}
    if handoff_staff is not None:
        staff[2] = handoff_staff
    if reply_staff is not None:
        staff[3] = reply_staff
    return messages, verdicts, staff


@pytest.mark.parametrize("handoff_staff,reply_staff", [
    (None, STAFF_KSENIA),      # подпись передавшего не привязана
    (STAFF_IRINA, None),       # подпись «Система» у ответившего
])
async def test_an_unattributed_author_after_a_handoff_is_the_specialists_contact(
    handoff_staff, reply_staff,
):
    """Неизвестно кто, но компания вышла на связь — ожидание специалиста закрыто."""
    messages, verdicts, staff = _case_handoff_and_ack_attribution(handoff_staff, reply_staff)
    items, _ = await replay(messages, verdicts, staff=staff, after=timedelta(days=2))
    assert items[1].substantive_at == messages[2].sent_at
    # В личную статистику неатрибутированная реплика не попадает.
    assert items[1].substantive_staff_id == reply_staff


async def test_the_handing_manager_does_not_close_his_own_wait():
    """«Принято» от самого передавшего вторым слоем не считается."""
    messages, verdicts, staff = _case_handoff_and_ack_attribution(STAFF_IRINA, STAFF_IRINA)
    items, _ = await replay(messages, verdicts, staff=staff, after=timedelta(days=2))
    assert items[1].substantive_at is None


async def test_two_waits_are_never_guessed():
    """При двух ожиданиях без ссылки движок не выбирает."""
    messages = [
        message(1, seconds=msk(12, 0), author=CLIENT_A, text="Нужен акт сверки"),
        message(2, seconds=msk(12, 1), author=CLIENT_A, text="И приглашение в банк"),
        message(3, COMPANY, seconds=msk(12, 3), text=IRINA + "Передала бухгалтеру"),
        message(4, COMPANY, seconds=msk(12, 4), text=IRINA + "И это передала"),
        message(5, COMPANY, seconds=msk(12, 30), text="Принял, смотрю"),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: REQUEST,
        3: (None, False, "handoff", 1), 4: (None, False, "handoff", 2),
        5: COMPANY_ACK,
    }, staff={3: STAFF_IRINA, 4: STAFF_IRINA}, after=timedelta(days=2))
    assert items[1].substantive_at is None
    assert items[2].substantive_at is None


async def test_a_handoff_never_closes_the_wait():
    """И от неизвестного автора передача — не выход специалиста."""
    messages = [
        message(1, seconds=msk(12, 0), author=CLIENT_A, text="Нужен акт сверки"),
        message(2, COMPANY, seconds=msk(12, 3), text=IRINA + "Передала бухгалтеру"),
        message(3, COMPANY, seconds=msk(12, 10), text="Передала ещё и Вере"),
    ]
    items, _ = await replay(messages, {1: REQUEST,
                                       2: (None, False, "handoff", 1),
                                       3: (None, False, "handoff", None)},
                            staff={2: STAFF_IRINA}, after=timedelta(days=2))
    assert items[1].substantive_at is None


# ── Вердикт есть, `is_substantive` пуст ──────────────────────────────────


def _case_handoff_then_follow_up(third_verdict):
    """Передача, а следом реплика передавшего менеджера со ссылкой."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A,
                text="Нужна сверка по НДС за 1 квартал"),
        message(2, COMPANY, seconds=msk(10, 5), text=IRINA + "Передала Лидии"),
        message(3, COMPANY, seconds=msk(10, 30), text=IRINA + "Ещё жду ответ Лидии"),
    ]
    verdicts = {1: REQUEST, 2: (None, False, "handoff", 1)}
    if third_verdict is not None:
        verdicts[3] = third_verdict
    return messages, verdicts


@pytest.mark.parametrize("third", [
    (None, None, None, 1),      # строка есть, всё пусто, ссылка есть
    (None, None, "ack", 1),     # метка есть, флага нет
])
async def test_a_blank_is_substantive_keeps_the_specialist_wait(third):
    """Пустое поле — сбой формата, а не ответ по существу."""
    messages, verdicts = _case_handoff_then_follow_up(third)
    items, _ = await replay(messages, verdicts,
                            staff={2: STAFF_IRINA, 3: STAFF_IRINA},
                            after=timedelta(days=2))
    assert items[1].substantive_at is None
    assert items[1].state is InteractionState.REACTED
    # Первой реакцией остаётся передача — первый слой адресуется транспортом.
    assert items[1].first_reaction_message_id == 2


async def test_a_message_with_no_verdict_row_keeps_the_wait():
    """Сообщение совсем без строки вердикта ожидание не закрывает."""
    messages, verdicts = _case_handoff_then_follow_up(None)
    items, _ = await replay(messages, verdicts,
                            staff={2: STAFF_IRINA, 3: STAFF_IRINA},
                            after=timedelta(days=2))
    assert items[1].substantive_at is None


async def test_a_promise_is_not_substantive():
    """`is_substantive=false` второй слой не закрывает."""
    messages, verdicts = _case_handoff_then_follow_up((None, False, "promise", 1))
    items, _ = await replay(messages, verdicts,
                            staff={2: STAFF_IRINA, 3: STAFF_IRINA},
                            after=timedelta(days=2))
    assert items[1].substantive_at is None


async def test_a_substantive_answer_closes_the_wait():
    """Явный `true` от специалиста закрывает ожидание."""
    messages, verdicts = _case_handoff_then_follow_up((None, True, "substantive", 1))
    items, _ = await replay(messages, verdicts,
                            staff={2: STAFF_IRINA, 3: STAFF_KSENIA},
                            after=timedelta(days=2))
    assert items[1].substantive_at == messages[2].sent_at


async def test_a_company_file_is_an_answer_by_itself():
    """Присланный компанией документ — ответ, даже без флага."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A, text="Нужна сверка по НДС"),
        message(2, COMPANY, seconds=msk(10, 5), text=IRINA + "Передала Лидии"),
        message(3, COMPANY, seconds=msk(10, 30), media="document",
                text=IRINA + "делится файлом"),
    ]
    items, _ = await replay(messages, {1: REQUEST,
                                       2: (None, False, "handoff", 1),
                                       3: (None, None, None, 1)},
                            staff={2: STAFF_IRINA, 3: STAFF_IRINA},
                            after=timedelta(days=2))
    assert items[1].substantive_at == messages[2].sent_at


# ── Граница версий правил ────────────────────────────────────────────────


def _case_across_the_v4_boundary():
    """Граница v4 в 10:15, до неё живёт обращение версии 3."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A, text="Нужна сверка по 62 счёту"),
        message(2, COMPANY, seconds=msk(10, 5), text=IRINA + "Передала бухгалтеру"),
        message(3, seconds=msk(10, 30), author=CLIENT_A, text="И ещё нужен акт за июль"),
        message(4, COMPANY, seconds=msk(10, 40), text=IRINA + "Акт за июль отправила"),
        message(5, COMPANY, seconds=msk(11, 0), text=KSENIA + "Сверка по 62 готова"),
        message(6, seconds=msk(11, 10), author=CLIENT_A,
                text="Поправка: акт нужен за сентябрь, а не за июль"),
        message(7, seconds=msk(11, 20), author=CLIENT_A,
                text="И отдельно нужна справка о численности"),
    ]
    verdicts = {
        1: REQUEST, 2: (None, False, "handoff", 1), 3: REQUEST,
        4: (None, True, "substantive", 3), 5: (None, True, "substantive", 1),
        6: CLIENT_CORRECTION, 7: REQUEST,
    }
    staff = {2: STAFF_IRINA, 4: STAFF_IRINA, 5: STAFF_KSENIA}
    return messages, verdicts, staff


async def test_a_v4_work_closed_by_an_older_branch_does_not_swallow_the_next_reply():
    """Обращение v4, закрытое веткой v1–v3, не воскресает и поправку не глотает."""
    messages, verdicts, staff = _case_across_the_v4_boundary()
    items, result = await replay(messages, verdicts, staff=staff,
                                 rules=(-86400, -86400, msk(10, 15)))
    assert members(result)[3] == [3]
    assert 6 in items
    assert items[6].opened_at == messages[5].sent_at
    assert result["interactions"] == 4


async def test_a_chat_entirely_under_v4_splits_the_same_way():
    """Без границы версий в чате тот же состав обращений."""
    messages, verdicts, staff = _case_across_the_v4_boundary()
    _, result = await replay(messages, verdicts, staff=staff,
                             rules=(-86400, -86400, -86400))
    assert result["interactions"] == 4
    assert members(result)[3] == [3]


# ── Эталонные сценарии второго слоя ──────────────────────────────────────


def _case_sibling_question():
    """Встречный вопрос специалиста со ссылкой на соседа по пачке."""
    return [
        message(6481, seconds=0, author=700000003,
                text="Добрый день. Мы обратились в банк и, чтобы дать вам доступ, "
                     "нам сказали, чтобы наш бухгалтер дал свои данные "
                     "и написал ФИО"),
        message(6482, seconds=87, author=700000004,
                text="Привет, у наших бухгалтеров есть доступы.\n\n"
                     "Или здесь мешают какие-то ограничения?"),
        message(6484, seconds=964, author=700000003, text="Доступа нет, как я поняла"),
        message(6502, COMPANY, seconds=52559,
                text=DARYA + "Доброе утро. Передала бухгалтеру письмо банка."),
        message(6528, COMPANY, seconds=57557,
                text=KSENIA + "Доброе утро. Раз от банка трудно получить точную "
                             "информацию, нужно сами чеки проанализировать. "
                             "Сможете дать нам доступ в ваш личный кабинет "
                             "вашего оператора ФД (Эвотор)?"),
    ]


_SIBLING_QUESTION_VERDICTS = {
    6481: REQUEST,
    6482: CLIENT_QUESTION,
    6484: CLIENT_QUESTION,
    6502: (None, False, "handoff", 6481),
    6528: (None, False, "question", 6484),
}
_SIBLING_QUESTION_STAFF = {6502: STAFF_DARYA, 6528: STAFF_KSENIA}


def _case_withdrawn_request():
    """Просьба → передача → отзыв клиентом → «принято»."""
    return [
        message(6711, seconds=0,
                text="Здравствуйте, а у Дмитрия номер заканчивается 0000? "
                     "Не могу дозвониться. Можете попросить её набрать меня?"),
        message(6716, COMPANY, seconds=95,
                text=IRINA + "Добрый день, передам ваш запрос"),
        message(6758, seconds=3082,
                text="Извините, уже не надо, вопрос решился сам, отбой, "
                     "не беспокойте Дмитрия)"),
        message(6762, COMPANY, seconds=3225, text=IRINA + "принято"),
    ]


_WITHDRAWN_REQUEST_VERDICTS = {
    6711: REQUEST,
    6716: (None, False, "handoff", 6711),
    6758: CLIENT_ACK,
    6762: (None, False, "ack", None),
}


def _fingerprint(items):
    """Всё, что правила второго слоя способны сдвинуть, — одной сравнимой выжимкой."""
    return {
        opener: (item.state, item.first_reaction_message_id, item.handoff_at,
                 item.substantive_at, item.substantive_staff_id,
                 item.sla_breached, item.substantive_breached, item.opened_at)
        for opener, item in items.items()
    }


async def test_a_counter_question_about_a_sibling_closes_the_specialist_wait():
    """Встречный вопрос другого специалиста со ссылкой на соседа по пачке
    засчитывается единственному ожиданию специалиста."""
    messages = _case_sibling_question()
    items, _ = await replay(messages, _SIBLING_QUESTION_VERDICTS,
                            staff=_SIBLING_QUESTION_STAFF, after=timedelta(hours=20))
    waiting = items[6481]
    assert waiting.handoff_at == messages[3].sent_at
    assert waiting.substantive_at == messages[4].sent_at
    assert waiting.substantive_breached is False
    assert waiting.state is InteractionState.REACTED
    assert items[6484].handoff_at is None and items[6484].substantive_at is None


async def test_a_withdrawn_request_closes_as_a_single_work():
    """Отзыв снимает ожидание специалиста и остаётся в составе своей работы."""
    items, result = await replay(_case_withdrawn_request(), _WITHDRAWN_REQUEST_VERDICTS,
                                 staff={6716: STAFF_IRINA}, after=timedelta(days=2))
    withdrawn = items[6711]
    assert withdrawn.state is InteractionState.NO_RESPONSE_NEEDED
    assert withdrawn.substantive_at is None
    assert withdrawn.first_reaction_message_id == 6716
    assert withdrawn.sla_breached is False
    assert result["interactions"] == 1
    assert members(result)[6711] == [6711, 6758]



# ── Соседние правила второго слоя и окна ─────────────────────────────────


async def test_a_sibling_result_from_another_specialist_closes_the_wait():
    """Результат другого специалиста со ссылкой на соседа по пачке засчитывается
    единственному ожиданию специалиста — как и встречный вопрос."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A,
                text="Банк отклоняет платёжку по новому контрагенту"),
        message(2, seconds=msk(10, 1), author=CLIENT_A, text="Что с лимитом по счёту?"),
        message(3, COMPANY, seconds=msk(10, 5),
                text=IRINA + "Передала бухгалтеру вопрос по банку"),
        message(4, COMPANY, seconds=msk(11, 30),
                text=KSENIA + "Банк подтвердил, ограничение снято, платёжка пройдёт"),
    ]
    items, _ = await replay(messages, {1: REQUEST, 2: CLIENT_QUESTION,
                                       3: (None, False, "handoff", 1),
                                       4: (None, True, "substantive", 2)},
                            staff={3: STAFF_IRINA, 4: STAFF_KSENIA},
                            after=timedelta(days=2))
    assert items[1].substantive_at == messages[3].sent_at
    assert items[1].substantive_staff_id == STAFF_KSENIA
    assert items[1].state is InteractionState.ANSWERED


async def test_a_company_request_for_material_makes_the_next_file_an_answer():
    """Просьба компании прислать материал без «?»: файл клиента следом — ответ."""
    messages = [
        message(1, COMPANY, seconds=msk(10, 0),
                text=IRINA + "Пришлите, пожалуйста, акты за июнь."),
        message(2, seconds=msk(10, 30), author=CLIENT_A, media="document"),
    ]
    _, result = await replay(messages, {1: COMPANY_SUBSTANTIVE}, staff={1: STAFF_IRINA})
    assert 2 in result["answers"]
    assert 2 not in result["response_required_ids"]


async def test_a_handoff_with_a_counter_question_arms_the_window():
    """Передача со встречным вопросом отдаёт ход клиенту: его реплика — ответ."""
    messages = [
        message(1, seconds=msk(12, 0), author=CLIENT_A,
                text="Не могу войти в личный кабинет, посмотрите"),
        message(2, COMPANY, seconds=msk(12, 3),
                text=IRINA + "Передала ваш запрос Ксении, подскажите, удалось войти?"),
        message(3, seconds=msk(12, 10), author=CLIENT_A,
                text="И ещё нужен акт сверки за август"),
    ]
    _, result = await replay(messages, {1: REQUEST, 2: (None, False, "handoff", 1),
                                        3: REQUEST}, staff={2: STAFF_IRINA})
    assert 3 in result["answers"]
