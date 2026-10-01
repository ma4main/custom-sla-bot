"""Только под `v4_mode`: новая просьба клиента не присоединяется к обращению,
которое закрыто по существу И чей срок реакции уже истёк, — она открывает своё
обращение. `addition`/`correction` по-прежнему присоединяются к своей работе.

Оба условия обязательны: без «закрыто» ответы внутри окна ожидания стали бы
обращениями, без «срок истёк» рвалась бы живая беседа. Метки — заданный вход.
"""

from datetime import timedelta

from tests.test_answer_window import QUESTION, replay
from tests.test_classify_context_v11 import COMPANY, REQUEST, message

ACK = (False, None, "ack", None)
ADDITION = (False, None, "addition", None)
SUBSTANTIVE = (None, True, "substantive", None)

# T0 — понедельник, 12:00 МСК. Календарь пн–пт 10:00–17:00,
# порог первой реакции 30 минут.
DAY = 86400
# Вторник, 11:00 МСК: ВНУТРИ окна ответа (оно живёт до вторника 12:00),
# но заведомо позже срока реакции вчерашнего обращения.
NEXT_DAY_MORNING = DAY - 3600


async def test_new_request_next_day_opens_its_own_work():
    """Три просьбы следующего дня — своё обращение, а не вчерашнее."""
    messages = [
        message(4196, COMPANY, seconds=0,
                text="Здесь клиент согласится пересдать декларацию и доплатить налог?"),
        message(4202, seconds=600, text="Этот вопрос сейчас обсуждаем с клиентом"),
        message(4617, seconds=NEXT_DAY_MORNING,
                text="Возьмите, пожалуйста, три счёта в работу (модуль)"),
    ]
    items, result = await replay(
        messages,
        {4196: (None, False, "question", None), 4202: ACK, 4617: REQUEST},
        after=timedelta(seconds=NEXT_DAY_MORNING + 3600),
    )
    # Просьба получила срок и СВОЁ обращение.
    assert 4617 in result["response_required_ids"]
    assert 4617 in items
    assert items[4617].opened_at == messages[2].sent_at
    # Вчерашний разговор её не поглотил: в нём по-прежнему одна реплика.
    assert items[4202].client_messages == 1


async def test_the_overdue_next_day_request_is_reported():
    """Тот же кейс целиком: 74 рабочих минуты молчания при пороге 30 дают просрочку."""
    late = NEXT_DAY_MORNING + 74 * 60
    messages = [
        message(4196, COMPANY, seconds=0,
                text="Здесь клиент согласится пересдать декларацию и доплатить налог?"),
        message(4202, seconds=600, text="Этот вопрос сейчас обсуждаем с клиентом"),
        message(4617, seconds=NEXT_DAY_MORNING,
                text="Возьмите, пожалуйста, три счёта в работу (модуль)"),
        message(4646, COMPANY, seconds=late, text="ПП в банке"),
    ]
    items, result = await replay(
        messages,
        {4196: (None, False, "question", None), 4202: ACK, 4617: REQUEST,
         4646: SUBSTANTIVE},
        after=timedelta(seconds=late + 600),
    )
    assert items[4617].sla_breached is True
    assert items[4617].ttfr_business_seconds == 74 * 60


async def test_addition_still_joins_yesterdays_accepted_work():
    """Дополнение к вчерашней принятой работе присоединяется без срока."""
    messages = [
        message(10, text="Возьмите, пожалуйста, три счёта в работу"),
        message(11, COMPANY, seconds=60, text="Принято, взяли в работу"),
        message(12, seconds=NEXT_DAY_MORNING, text="Вот сумма по второму счёту: 12 400"),
    ]
    items, result = await replay(
        messages,
        {10: REQUEST, 11: (None, False, "ack", 10), 12: ADDITION},
        after=timedelta(seconds=NEXT_DAY_MORNING + 3600),
    )
    assert 12 not in result["response_required_ids"]
    assert 12 not in items          # своего обращения дополнение не открыло
    assert items[10].client_messages == 2


async def test_second_request_five_minutes_after_its_own_gets_its_own_deadline():
    """Просьба вслед за своей же открытой просьбой получает свой срок.

    Обращение, где ждут ответа, закрытым не считается, и правило его не режет.
    """
    messages = [
        message(10, text="Возьмите, пожалуйста, три счёта в работу"),
        message(11, seconds=300, text="И ещё третий счёт выставьте, пожалуйста"),
    ]
    items, result = await replay(messages, {10: REQUEST, 11: REQUEST})
    assert {10, 11} <= set(result["response_required_ids"])
    assert items[11].opened_at == messages[1].sent_at
    assert items[10].client_messages == 1


async def test_an_answer_series_inside_the_window_is_not_cut():
    """Окно возобновления: пока срок вчерашнего разговора не истёк, правило молчит.

    «Да.» через минуту, «Узнаю у какого банка проще .» через шесть.
    Обращение из одних ответов формально «закрыто», но его собственный срок
    реакции ещё идёт: беседа живая, и вторая реплика остаётся ответом.
    """
    messages = [
        message(2858, COMPANY, seconds=0, text="вы же напишете по итогу?"),
        message(2859, seconds=60, text="Да."),
        message(2860, seconds=360, text="Узнаю у какого банка проще ."),
    ]
    _, result = await replay(
        messages,
        {2858: (None, False, "question", None), 2859: (False, None, "answer", None),
         2860: QUESTION},
    )
    assert 2860 not in result["response_required_ids"]


async def test_a_client_answer_twelve_minutes_after_the_question():
    """Ответ клиента через двенадцать минут — не обращение, даже с меткой `request`."""
    messages = [
        message(4514, COMPANY, seconds=0,
                text="Добрый день. Олег, подскажите по готовности справок"),
        message(4534, seconds=720,
                text="Добрый день, отправил на почту два банка, "
                     "третий банк — пока справки не пришло"),
    ]
    _, result = await replay(messages, {4514: (None, False, "question", None), 4534: REQUEST})
    assert 4534 not in result["response_required_ids"]
